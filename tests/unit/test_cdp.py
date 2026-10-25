from __future__ import annotations

import json
import os
import random
import threading

import pytest

from aevrel.cdp import (
    CDPError,
    CheckpointFollower,
    ChunkingPolicy,
    ChunkStore,
    FenceStore,
    ManifestStore,
    VersionTarget,
    VersionMarker,
    build_manifest,
    build_manifest_from_files,
    build_manifest_from_paths,
    delta_manifest,
    materialize_manifest,
    read_marker,
    scan_versions,
    verify_manifest,
    write_marker,
)


def make_version(
    root,
    version_id,
    files: dict[str, bytes],
    *,
    parent_id: str | None = None,
    complete: bool = True,
):
    version_dir = root / version_id
    version_dir.mkdir(parents=True)
    for name, data in files.items():
        path = version_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    marker = VersionMarker(
        version="1",
        version_id=version_id,
        job_uid="job-1",
        step=int(version_id.removeprefix("step-") or 0),
        parent_id=parent_id,
        metadata_hash="sha256:" + "a" * 64,
        created_at=f"2026-10-22T00:00:{len(version_id):02d}Z",
        complete=complete,
    )
    write_marker(version_dir, marker)
    return version_dir


def test_scan_versions_ignores_incomplete_versions(tmp_path) -> None:
    make_version(tmp_path, "step-100", {"a.bin": b"one"})
    make_version(
        tmp_path,
        "step-200",
        {"a.bin": b"two"},
        parent_id="step-100",
        complete=False,
    )

    complete = scan_versions(tmp_path)
    assert [item.version_id for item in complete] == ["step-100"]

    all_versions = scan_versions(tmp_path, include_incomplete=True)
    assert [item.version_id for item in all_versions] == ["step-100", "step-200"]
    assert all_versions[1].complete is False


def test_scan_versions_rejects_duplicate_version_ids(tmp_path) -> None:
    make_version(tmp_path, "step-100", {"a.bin": b"one"})
    other = tmp_path / "other"
    other.mkdir()
    marker = read_marker(tmp_path / "step-100")
    write_marker(other, marker)

    with pytest.raises(CDPError, match="duplicate checkpoint version"):
        scan_versions(tmp_path)


def test_manifest_delta_transfers_only_changed_chunks(tmp_path) -> None:
    make_version(tmp_path, "step-100", {"a.bin": b"abcdefgh", "b.bin": b"xxxx"})
    make_version(
        tmp_path,
        "step-200",
        {"a.bin": b"abcdefgh", "b.bin": b"yyyy"},
        parent_id="step-100",
    )
    versions = scan_versions(tmp_path)
    store = ChunkStore(tmp_path / "target")
    first = build_manifest(versions[0], store, chunk_bytes=4)
    second = build_manifest(versions[1], store, chunk_bytes=4)

    delta = delta_manifest(first, second)
    assert delta.base_version == "step-100"
    assert delta.version_id == "step-200"
    assert delta.total_bytes == 12
    assert delta.missing_bytes == 4
    assert delta.reused_bytes == 8
    assert len(delta.missing) == 1
    assert len(delta.reused) == 2
    assert delta.delta_ratio == pytest.approx(1 / 3)


def test_manifest_dedups_identical_content_across_files(tmp_path) -> None:
    make_version(tmp_path, "step-100", {"a.bin": b"same", "b.bin": b"same"})
    descriptor = scan_versions(tmp_path)[0]
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest(descriptor, store, chunk_bytes=4)

    assert len(manifest.chunks) == 2
    assert len({chunk.digest for chunk in manifest.chunks}) == 1
    assert delta_manifest(None, manifest).total_bytes == 4


def test_content_defined_chunking_keeps_chunk_bounds(tmp_path) -> None:
    data = random.Random(7).randbytes(16 * 1024)
    source = tmp_path / "a.bin"
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": source},
        chunk_store=store,
        chunking=ChunkingPolicy.content_defined(
            min_bytes=128,
            avg_bytes=512,
            max_bytes=2048,
        ),
    )
    sizes = [chunk.length for chunk in manifest.chunks]
    assert sizes[0] >= 0
    assert sum(sizes) == len(data)
    assert all(128 <= size <= 2048 for size in sizes[:-1])
    assert sizes[-1] <= 2048


def test_content_defined_delta_reuses_unchanged_content(tmp_path) -> None:
    data = random.Random(11).randbytes(32 * 1024)
    changed = data[:4096] + b"X" + data[4097:]
    policy = ChunkingPolicy.content_defined(
        min_bytes=128,
        avg_bytes=512,
        max_bytes=2048,
    )
    store = ChunkStore(tmp_path / "target")
    first = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"rank0/a.bin": data},
        chunk_store=store,
        chunking=policy,
    )
    second = build_manifest_from_files(
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:01Z",
        files={"rank0/a.bin": changed},
        chunk_store=store,
        chunking=policy,
    )

    delta = delta_manifest(first, second)
    assert delta.reused_bytes > 0
    assert delta.missing_bytes < delta.total_bytes // 2


def test_canonical_chunks_are_independent_of_target_file_names(tmp_path) -> None:
    data = random.Random(21).randbytes(8 * 1024)
    policy = ChunkingPolicy.content_defined(
        min_bytes=128,
        avg_bytes=512,
        max_bytes=2048,
    )
    store = ChunkStore(tmp_path / "target")
    layout_a = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"rank0/w.distcp": data},
        chunk_store=store,
        chunking=policy,
    )
    layout_b = build_manifest_from_files(
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:01Z",
        files={"rank1/w.distcp": data},
        chunk_store=store,
        chunking=policy,
    )

    delta = delta_manifest(layout_a, layout_b)
    assert delta.missing_bytes == 0
    assert delta.reused_bytes == delta.total_bytes


def test_streaming_cdc_chunks_roundtrip(tmp_path) -> None:
    data = random.Random(31).randbytes(64 * 1024)
    source = tmp_path / "a.bin"
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": source},
        chunk_store=store,
        chunking=ChunkingPolicy.content_defined(
            min_bytes=128,
            avg_bytes=512,
            max_bytes=2048,
        ),
    )
    assert all(chunk.length <= 2048 for chunk in manifest.chunks)
    output = tmp_path / "out"
    materialize_manifest(manifest, store, output)
    assert (output / "a.bin").read_bytes() == data


def test_streaming_manifest_matches_fixed_manifest(tmp_path) -> None:
    data = random.Random(41).randbytes(4096)
    file_path = tmp_path / "a.bin"
    file_path.write_bytes(data)
    store = ChunkStore(tmp_path / "target")
    from_bytes = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": data},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(128),
    )
    from_path = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": file_path},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(128),
    )
    assert from_path == from_bytes


def test_streaming_cdc_manifest_materializes_bitwise(tmp_path) -> None:
    data = random.Random(51).randbytes(32 * 1024)
    file_path = tmp_path / "a.bin"
    file_path.write_bytes(data)
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": file_path},
        chunk_store=store,
        chunking=ChunkingPolicy.content_defined(
            min_bytes=128,
            avg_bytes=512,
            max_bytes=2048,
        ),
    )
    output = tmp_path / "out"
    materialize_manifest(manifest, store, output)
    assert (output / "a.bin").read_bytes() == data


def test_file_chunking_streams_whole_file_to_cas(tmp_path) -> None:
    data = random.Random(61).randbytes(16 * 1024)
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"rank0/w.distcp": source},
        chunk_store=store,
        chunking=ChunkingPolicy.file(),
    )
    assert len(manifest.chunks) == 1
    assert verify_manifest(manifest, store) == len(data)
    output = tmp_path / "output"
    materialize_manifest(manifest, store, output)
    assert (output / "rank0" / "w.distcp").read_bytes() == data


def test_native_file_ingest_can_hardlink_immutable_source(tmp_path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"same-inode")
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"rank0/source.bin": source},
        chunk_store=store,
        chunking=ChunkingPolicy.file(),
        hardlink=True,
    )
    assert len(manifest.chunks) == 1
    digest = manifest.chunks[0].digest
    assert store.read(digest) == b"same-inode"
    if os.name != "nt":
        assert store.path(digest).stat().st_ino == source.stat().st_ino


def test_file_chunking_delta_only_changed_file(tmp_path) -> None:
    store = ChunkStore(tmp_path / "target")
    first = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"same.bin": b"same", "changed.bin": b"old"},
        chunk_store=store,
        chunking=ChunkingPolicy.file(),
    )
    second = build_manifest_from_files(
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:01Z",
        files={"same.bin": b"same", "changed.bin": b"new"},
        chunk_store=store,
        chunking=ChunkingPolicy.file(),
    )
    delta = delta_manifest(first, second)
    assert delta.missing_bytes == len(b"new")
    assert delta.reused_bytes == len(b"same")


def test_incremental_materialize_reuses_unchanged_file(tmp_path) -> None:
    store = ChunkStore(tmp_path / "target")
    first = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"keep.bin": b"same-content"},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(4),
    )
    second = build_manifest_from_files(
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:01Z",
        files={"keep.bin": b"same-content"},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(4),
    )
    base_root = tmp_path / "base"
    output = tmp_path / "output"
    materialize_manifest(first, store, base_root)
    written = materialize_manifest(
        second,
        store,
        output,
        base_manifest=first,
        base_root=base_root,
    )
    assert written == 0
    assert (output / "keep.bin").read_bytes() == b"same-content"


def test_incremental_materialize_patches_changed_file(tmp_path) -> None:
    store = ChunkStore(tmp_path / "target")
    base_data = b"abcdefghijkl"
    changed_data = b"abcdWXYZijkl"
    first = build_manifest_from_files(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": base_data},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(4),
    )
    second = build_manifest_from_files(
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:01Z",
        files={"a.bin": changed_data},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(4),
    )
    base_root = tmp_path / "base"
    output = tmp_path / "output"
    materialize_manifest(first, store, base_root)
    written = materialize_manifest(
        second,
        store,
        output,
        base_manifest=first,
        base_root=base_root,
    )
    assert written <= 4
    assert (output / "a.bin").read_bytes() == changed_data


def test_manifest_store_roundtrip_and_root_validation(tmp_path) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"data"})
    descriptor = scan_versions(tmp_path / "source")[0]
    chunks = ChunkStore(tmp_path / "target")
    manifest = build_manifest(descriptor, chunks, chunk_bytes=4)

    manifests = ManifestStore(tmp_path / "target")
    manifests.write(manifest)
    loaded = manifests.read("step-100")
    assert loaded == manifest
    assert verify_manifest(loaded, chunks) == 4

    manifest_path = manifests.path("step-100")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["root"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CDPError, match="root"):
        manifests.read("step-100")


def test_materialize_manifest_reconstructs_files(tmp_path) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"abcdefgh"})
    descriptor = scan_versions(tmp_path / "source")[0]
    chunks = ChunkStore(tmp_path / "target")
    manifest = build_manifest(descriptor, chunks, chunk_bytes=4)
    output = tmp_path / "shadow"

    written = materialize_manifest(manifest, chunks, output)
    assert written == 8
    assert (output / "a.bin").read_bytes() == b"abcdefgh"

    with pytest.raises(CDPError, match="already exists"):
        materialize_manifest(manifest, chunks, output)
    materialize_manifest(manifest, chunks, output, overwrite=True)

    chunk_path = chunks.path(manifest.chunks[0].digest)
    chunk_path.write_bytes(b"bad!")
    with pytest.raises(CDPError, match="digest mismatch"):
        verify_manifest(manifest, chunks)


def test_build_manifest_rejects_incomplete_version(tmp_path) -> None:
    make_version(tmp_path, "step-100", {"a.bin": b"data"}, complete=False)
    descriptor = scan_versions(tmp_path, include_incomplete=True)[0]
    with pytest.raises(CDPError, match="incomplete"):
        build_manifest(descriptor, ChunkStore(tmp_path / "target"))


def test_version_target_promotes_and_rolls_back_atomically(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(source, "step-200", {"a.bin": b"v2-data"}, parent_id="step-100")
    first_descriptor, second_descriptor = scan_versions(source)
    target = VersionTarget(tmp_path / "target")
    first = build_manifest(first_descriptor, target.chunks, chunk_bytes=4)
    second = build_manifest(second_descriptor, target.chunks, chunk_bytes=4)

    pointer = target.promote(first)
    assert pointer.version_id == "step-100"
    assert target.current().version_id == "step-100"
    assert (target.shadow / "step-100" / "a.bin").read_bytes() == b"v1-data"

    delta = delta_manifest(first, second)
    assert delta.missing_bytes > 0
    target.promote(second)
    assert target.current().version_id == "step-200"
    assert (target.shadow / "step-200" / "a.bin").read_bytes() == b"v2-data"

    target.rollback("step-100")
    assert target.current().version_id == "step-100"
    assert (target.shadow / "step-200" / "a.bin").read_bytes() == b"v2-data"


def test_follower_stages_delta_without_switching_current(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(source, "step-200", {"a.bin": b"v2-data"}, parent_id="step-100")
    target = VersionTarget(tmp_path / "target")
    follower = CheckpointFollower(source, target, chunk_bytes=4)

    first_pass = follower.sync_once()
    assert [item.version_id for item in first_pass] == ["step-100", "step-200"]
    assert first_pass[0].staged is True
    assert first_pass[1].staged is True
    assert first_pass[1].missing_bytes > 0
    assert target.current() is None
    assert (target.shadow / "step-100" / "a.bin").read_bytes() == b"v1-data"
    assert (target.shadow / "step-200" / "a.bin").read_bytes() == b"v2-data"

    second_pass = follower.sync_once()
    assert all(item.staged is False for item in second_pass)

    target.promote(target.manifests.read("step-200"))
    assert target.current().version_id == "step-200"


def test_prune_keeps_recent_versions_and_collects_unreferenced_chunks(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    make_version(
        source,
        "step-100",
        {"shared.bin": b"same", "old.bin": b"old!"},
    )
    make_version(
        source,
        "step-200",
        {"shared.bin": b"same", "new.bin": b"new!"},
        parent_id="step-100",
    )
    make_version(
        source,
        "step-300",
        {"shared.bin": b"same", "new.bin": b"new!"},
        parent_id="step-200",
    )
    descriptors = scan_versions(source)
    target = VersionTarget(tmp_path / "target")
    manifests = [
        build_manifest(descriptor, target.chunks, chunk_bytes=4)
        for descriptor in descriptors
    ]
    for manifest in manifests:
        target.stage(manifest)

    old_digest = next(
        chunk.digest for chunk in manifests[0].chunks if chunk.file == "old.bin"
    )
    shared_digest = next(
        chunk.digest for chunk in manifests[0].chunks if chunk.file == "shared.bin"
    )
    result = target.prune(2)

    assert result.retained_versions == ("step-200", "step-300")
    assert result.deleted_versions == ("step-100",)
    assert result.deleted_chunks == 1
    assert result.freed_bytes == 4
    assert target.chunks.has(shared_digest)
    assert not target.chunks.has(old_digest)
    assert not target.manifests.path("step-100").exists()
    assert not target._shadow_path("step-100").exists()
    assert target.manifests.path("step-200").exists()
    assert target.manifests.path("step-300").exists()

    second = target.prune(2)
    assert second.deleted_versions == ()
    assert second.deleted_chunks == 0


def test_prune_always_keeps_current_version(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"one!"})
    make_version(
        source,
        "step-200",
        {"a.bin": b"two!"},
        parent_id="step-100",
    )
    make_version(
        source,
        "step-300",
        {"a.bin": b"thre"},
        parent_id="step-200",
    )
    descriptors = scan_versions(source)
    target = VersionTarget(tmp_path / "target")
    manifests = [
        build_manifest(descriptor, target.chunks, chunk_bytes=4)
        for descriptor in descriptors
    ]
    target.promote(manifests[0])
    target.stage(manifests[1])
    target.stage(manifests[2])

    result = target.prune(1)

    assert result.retained_versions == ("step-100", "step-300")
    assert result.deleted_versions == ("step-200",)
    assert target.current().version_id == "step-100"
    assert target._shadow_path("step-100").is_dir()
    assert target._shadow_path("step-300").is_dir()


def test_prune_keeps_fenced_version(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"one!"})
    make_version(
        source,
        "step-200",
        {"a.bin": b"two!"},
        parent_id="step-100",
    )
    make_version(
        source,
        "step-300",
        {"a.bin": b"thre"},
        parent_id="step-200",
    )
    descriptors = scan_versions(source)
    target = VersionTarget(tmp_path / "target")
    manifests = [
        build_manifest(descriptor, target.chunks, chunk_bytes=4)
        for descriptor in descriptors
    ]
    for manifest in manifests:
        target.stage(manifest)
    FenceStore(target.root).acquire("controller-a", 1, "step-100")

    result = target.prune(1)

    assert result.retained_versions == ("step-100", "step-300")
    assert result.deleted_versions == ("step-200",)
    assert target._shadow_path("step-100").is_dir()


def test_prune_aborts_before_deleting_when_retained_chunk_is_missing(
    tmp_path,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"one!"})
    make_version(
        source,
        "step-200",
        {"a.bin": b"two!"},
        parent_id="step-100",
    )
    descriptors = scan_versions(source)
    target = VersionTarget(tmp_path / "target")
    manifests = [
        build_manifest(descriptor, target.chunks, chunk_bytes=4)
        for descriptor in descriptors
    ]
    for manifest in manifests:
        target.stage(manifest)
    target.chunks.path(manifests[1].chunks[0].digest).unlink()

    with pytest.raises(CDPError, match="missing chunk"):
        target.prune(1)

    assert target.manifests.path("step-100").is_file()
    assert target.manifests.path("step-200").is_file()
    assert target._shadow_path("step-100").is_dir()


def test_prune_rejects_non_positive_retention(tmp_path) -> None:
    target = VersionTarget(tmp_path / "target")
    with pytest.raises(ValueError, match="retain_versions"):
        target.prune(0)


def test_fence_store_rejects_stale_epoch_and_owner(tmp_path) -> None:
    fences = FenceStore(tmp_path / "target")
    first = fences.acquire("controller-a", 1, "step-100")
    assert fences.current() == first
    fences.validate(first)

    second = fences.acquire("controller-b", 2, "step-200")
    with pytest.raises(CDPError, match="stale"):
        fences.validate(first)
    fences.validate(second)

    with pytest.raises(CDPError, match="stale fence epoch"):
        fences.acquire("controller-c", 1, "step-300")
    with pytest.raises(CDPError, match="owned by"):
        fences.acquire("controller-c", 2, "step-300")


def test_fence_acquire_is_cross_thread_serialized(tmp_path) -> None:
    fences = FenceStore(tmp_path / "target")
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def worker(owner: str) -> None:
        barrier.wait()
        try:
            fences.acquire(owner, 1, "step-100")
            results.append(("ok", owner))
        except CDPError as exc:
            results.append(("error", str(exc)))

    threads = [
        threading.Thread(target=worker, args=(owner,))
        for owner in ("controller-a", "controller-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert [status for status, _ in results].count("ok") == 1
    assert len(results) == 2


def test_corrupt_cas_blocks_stage_and_keeps_current_unchanged(tmp_path) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"v1-data"})
    descriptor = scan_versions(tmp_path / "source")[0]
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest(descriptor, store, chunk_bytes=4)
    target = VersionTarget(tmp_path / "target")

    chunk_path = store.path(manifest.chunks[0].digest)
    chunk_path.write_bytes(b"corrupt")
    with pytest.raises(CDPError, match="digest mismatch"):
        target.stage(manifest)
    assert target.current() is None


def test_failed_shadow_publish_does_not_publish_current(tmp_path, monkeypatch) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"v1-data"})
    descriptor = scan_versions(tmp_path / "source")[0]
    target = VersionTarget(tmp_path / "target")
    store = ChunkStore(target.root)
    manifest = build_manifest(descriptor, store, chunk_bytes=4)

    def fail_materialize(*args, **kwargs):
        raise CDPError("injected shadow write failure")

    monkeypatch.setattr("aevrel.cdp.materialize_manifest", fail_materialize)
    with pytest.raises(CDPError, match="shadow write failure"):
        target.promote(manifest)
    assert target.current() is None


def test_corrupt_cas_blocks_stage_and_keeps_current_unchanged(tmp_path) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"v1-data"})
    descriptor = scan_versions(tmp_path / "source")[0]
    store = ChunkStore(tmp_path / "target")
    manifest = build_manifest(descriptor, store, chunk_bytes=4)
    target = VersionTarget(tmp_path / "target")

    chunk_path = store.path(manifest.chunks[0].digest)
    chunk_path.write_bytes(b"corrupt")
    with pytest.raises(CDPError, match="digest mismatch"):
        target.stage(manifest)
    assert target.current() is None


def test_failed_shadow_publish_does_not_publish_current(tmp_path, monkeypatch) -> None:
    make_version(tmp_path / "source", "step-100", {"a.bin": b"v1-data"})
    descriptor = scan_versions(tmp_path / "source")[0]
    target = VersionTarget(tmp_path / "target")
    store = ChunkStore(target.root)
    manifest = build_manifest(descriptor, store, chunk_bytes=4)

    def fail_materialize(*args, **kwargs):
        raise CDPError("injected shadow write failure")

    monkeypatch.setattr("aevrel.cdp.materialize_manifest", fail_materialize)
    with pytest.raises(CDPError, match="shadow write failure"):
        target.promote(manifest)
    assert target.current() is None
