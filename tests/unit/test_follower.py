from __future__ import annotations

import threading
import time

from shardferry.cdp import (
    CheckpointFollower,
    ChunkingPolicy,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from shardferry.follower import FollowerLoop


def make_version(root, version_id, files, *, parent_id=None) -> None:
    version_dir = root / version_id
    version_dir.mkdir(parents=True)
    for name, data in files.items():
        path = version_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    write_marker(
        version_dir,
        VersionMarker(
            version="1",
            version_id=version_id,
            job_uid="job-1",
            step=int(version_id.removeprefix("step-")),
            parent_id=parent_id,
            metadata_hash="sha256:" + "a" * 64,
            created_at=f"2026-10-22T00:00:{len(version_id):02d}Z",
            complete=True,
        ),
    )


def test_follower_loop_catches_up_one_version_per_cycle(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1"})
    make_version(source, "step-200", {"a.bin": b"v2"}, parent_id="step-100")
    make_version(source, "step-300", {"a.bin": b"v3"}, parent_id="step-200")
    target = VersionTarget(tmp_path / "target")
    loop = FollowerLoop(
        source,
        target,
        chunk_bytes=4,
        max_versions_per_cycle=1,
        poll_interval=0,
    )

    assert loop.step() == 1
    status = loop.status()
    assert status.state == "catching-up"
    assert status.lag_versions == 2
    assert status.target_latest == "step-100"
    assert status.synced_versions == 1

    assert loop.step() == 1
    assert loop.status().target_latest == "step-200"
    assert loop.status().lag_versions == 1

    assert loop.step() == 1
    status = loop.status()
    assert status.state == "following"
    assert status.lag_versions == 0
    assert status.target_latest == "step-300"
    assert status.synced_versions == 3

    assert loop.step() == 0
    assert loop.status().last_error is None


def test_follower_loop_records_error_and_recovers(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-200", {"a.bin": b"v2"}, parent_id="step-100")
    target = VersionTarget(tmp_path / "target")
    loop = FollowerLoop(source, target, chunk_bytes=4, poll_interval=0)

    assert loop.step() == 0
    assert loop.status().state == "error"
    assert "parent version" in (loop.status().last_error or "")

    make_version(source, "step-100", {"a.bin": b"v1"})
    assert loop.step() == 1
    assert loop.status().state == "catching-up"
    assert loop.status().last_error is None
    assert loop.status().target_latest == "step-100"

    assert loop.step() == 1
    assert loop.status().target_latest == "step-200"


def test_follower_uses_target_layout_file_provider(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"source.bin": b"target-data"})
    target = VersionTarget(tmp_path / "target")

    def provider(descriptor):
        return {
            "rank0/w.distcp": (descriptor.path / "source.bin").read_bytes(),
        }

    follower = CheckpointFollower(
        source,
        target,
        chunking=ChunkingPolicy.fixed(4),
        file_provider=provider,
    )
    follower.sync_once()
    manifest = target.manifests.read("step-100")
    assert {chunk.file for chunk in manifest.chunks} == {"rank0/w.distcp"}
    assert (target.shadow / "step-100" / "rank0" / "w.distcp").read_bytes() == (
        b"target-data"
    )


def test_follower_status_does_not_wait_for_a_running_sync(tmp_path) -> None:
    source = tmp_path / "source"
    started = threading.Event()
    release = threading.Event()

    class BlockingFollower:
        source_root = source

        def sync_once(self, *, max_versions=None):
            del max_versions
            started.set()
            release.wait(timeout=2.0)
            return []

    loop = FollowerLoop(
        source,
        VersionTarget(tmp_path / "target"),
        follower=BlockingFollower(),
        lock=threading.RLock(),
        poll_interval=0,
    )
    worker = threading.Thread(target=loop.step)
    worker.start()
    try:
        assert started.wait(timeout=1.0)
        before = time.monotonic()
        status = loop.status()
        assert time.monotonic() - before < 0.2
        assert status.state == "starting"
    finally:
        release.set()
        worker.join(timeout=2.0)
