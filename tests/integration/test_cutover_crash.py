from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shardferry.cdp import (
    CheckpointFollower,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from shardferry.cutover import CutoverPolicy, CutoverSession


def make_version(root: Path, version_id: str, files: dict[str, bytes]) -> None:
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
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )


class RecordingHook:
    def __init__(self) -> None:
        self.events: list[str] = []

    def pause(self, version_id: str) -> None:
        self.events.append(f"pause:{version_id}")

    def resume(self) -> None:
        self.events.append("resume")


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL harness is POSIX-only")
@pytest.mark.parametrize("mode", ["pause", "delta", "promote", "rollback"])
def test_cutover_kill_recovery(tmp_path: Path, mode: str) -> None:
    root = tmp_path / mode
    source = root / "source"
    target_root = root / "target"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    version = "step-100"
    if mode == "rollback":
        make_version(source, "step-200", {"a.bin": b"v2-data"})
        version = "step-200"
    target = VersionTarget(target_root)
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    if mode == "rollback":
        target.promote(target.manifests.read("step-100"))

    worker = Path(__file__).resolve().parents[1] / "fixtures" / "cutover_crash_worker.py"
    events = root / "events.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            "--source",
            str(source),
            "--target",
            str(target_root),
            "--version",
            version,
            "--mode",
            mode,
            "--events",
            str(events),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == -9, result.stderr

    hook = RecordingHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    recovered = session.recover(owner="controller-a")

    if mode in {"pause", "delta"}:
        assert recovered.state == "resumed"
        assert recovered.resumed is True
        assert target.current() is None
        assert hook.events == ["resume"]
    else:
        assert recovered.state == "promoted"
        assert target.current() is not None
        assert target.current().version_id == version
        assert hook.events == []
