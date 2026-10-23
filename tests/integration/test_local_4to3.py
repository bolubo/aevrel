from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from shardferry import executor
from shardferry.executor import ExecutionError, apply, commit_staged
from shardferry.planning import plan
from shardferry.storage.file import FileStorage, StorageError
from shardferry.targets import journal_path, target_file_path
from shardferry.verification import VerificationError, verify
from tests.fixtures.generated import (
    generate_partial_source_spec,
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import assert_bitwise_equal, reference_reshard


def _case(tmp_path: Path):
    spec, source_arrays, tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    return spec, tensor


def _source_bytes(spec: dict) -> dict[str, bytes]:
    storage = FileStorage()
    return {
        shard["uri"]: storage.path(shard["uri"]).read_bytes()
        for shard in spec["source"]["shards"]
    }


def test_local_4to3_apply_and_verify(tmp_path) -> None:
    spec, tensor = _case(tmp_path)
    source_before = _source_bytes(spec)
    result = plan(spec)

    report = apply(result, spec)
    assert report["status"] == "success"
    assert report["bytes_moved"] == result["total_bytes"]
    assert report["verification"]["passed"] is True
    assert report["meshes"]["source"]["world_size"] == 4
    assert report["meshes"]["target"]["world_size"] == 3
    assert report["chunks"] == len(result["nodes"]) == 8
    assert report["tensors"][0]["chunks"] == 8

    verify(spec, result)
    expected = reference_reshard(tensor, (3, 1), ("Shard(0)", "Replicate()"))
    for rank, (_, expected_array) in expected.items():
        actual = np.frombuffer(
            target_file_path(spec, "w", rank).read_bytes(),
            dtype=np.float32,
        ).reshape(expected_array.shape)
        assert_bitwise_equal(actual, expected_array)
    assert _source_bytes(spec) == source_before


def test_stage_then_commit_is_two_phase(tmp_path) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)

    staged = apply(result, spec, commit=False)

    assert staged["status"] == "staged"
    assert not target_file_path(spec, "w", 0).exists()
    committed = commit_staged(result, spec)
    assert committed["status"] == "success"
    assert target_file_path(spec, "w", 0).is_file()
    verify(spec, result)


def test_interrupted_apply_resumes(tmp_path, monkeypatch) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    original = executor._apply_node
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        if calls["count"] >= 4:
            raise RuntimeError("injected interruption")
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_apply_node", flaky)
    with pytest.raises(RuntimeError, match="injected"):
        apply(result, spec)
    journal = journal_path(spec, result["migration_id"])
    assert journal.is_file()
    assert len(json.loads(journal.read_text())["completed_nodes"]) == 4

    monkeypatch.setattr(executor, "_apply_node", original)
    report = apply(result, spec)
    assert report["status"] == "success"
    assert report["retries"] >= 1
    verify(spec, result)


def test_verify_detects_corrupt_target(tmp_path) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    apply(result, spec)
    path = target_file_path(spec, "w", 0)
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    path.write_bytes(data)
    with pytest.raises(VerificationError):
        verify(spec, result)


def test_dry_run_does_not_create_targets(tmp_path) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    report = apply(result, spec, dry_run=True)
    assert report["verification"]["method"] == "checksum"
    assert not target_file_path(spec, "w", 0).exists()


def test_source_checksum_mismatch_fails_before_target(tmp_path) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    source_path = FileStorage().path(spec["source"]["shards"][0]["uri"])
    source_path.write_bytes(source_path.read_bytes() + b"corrupt")
    with pytest.raises(StorageError, match="checksum mismatch"):
        apply(result, spec)
    assert not target_file_path(spec, "w", 0).exists()


def test_partial_source_apply(tmp_path) -> None:
    spec, source_arrays, tensor = generate_partial_source_spec(
        (4, 2), 2, (2,), ("Shard(0)",)
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    result = plan(spec)
    apply(result, spec)
    verify(spec, result)
    expected = reference_reshard(tensor, (2,), ("Shard(0)",))
    for rank, (_, expected_array) in expected.items():
        actual = np.frombuffer(
            target_file_path(spec, "w", rank).read_bytes(),
            dtype=np.float32,
        ).reshape(expected_array.shape)
        assert_bitwise_equal(actual, expected_array)


def test_target_write_failure_leaves_source_unchanged(
    tmp_path, monkeypatch
) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    before = _source_bytes(spec)
    original = FileStorage.write_range
    original_copy = FileStorage.copy_range

    def fail_write(self, uri, offset, data):
        raise StorageError("injected disk failure")

    def fail_copy(self, *args, **kwargs):
        raise StorageError("injected disk failure")

    monkeypatch.setattr(FileStorage, "write_range", fail_write)
    monkeypatch.setattr(FileStorage, "copy_range", fail_copy)
    with pytest.raises(StorageError, match="injected"):
        apply(result, spec)
    monkeypatch.setattr(FileStorage, "write_range", original)
    monkeypatch.setattr(FileStorage, "copy_range", original_copy)
    assert _source_bytes(spec) == before
    assert not target_file_path(spec, "w", 0).exists()


def test_target_path_conflict_fails_before_source_transfer(tmp_path) -> None:
    spec, _ = _case(tmp_path)
    result = plan(spec)
    bad_root = tmp_path / "bad-target"
    bad_root.mkdir()
    (bad_root / "w").write_text("block", encoding="ascii")
    with pytest.raises(ExecutionError, match="target parent is not a directory"):
        apply(result, spec, target_root=bad_root)
