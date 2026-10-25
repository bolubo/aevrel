"""Golden equivalence for the native chunker.

The fixture ``tests/fixtures/golden_manifests.json`` was captured from the
pre-refactor Python implementation. The Rust-backed builders must reproduce it
byte-for-byte, proving the migration changed no chunk identity, digest or
manifest root.

Input data is deterministic pseudo-random so that content-defined cuts really
occur; small uniform data only exercises max_bytes/EOF cuts and would not
discriminate between chunking implementations.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from aevrel.cdp import (
    ChunkStore,
    ChunkingPolicy,
    build_manifest_from_paths,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden_manifests.json"

META = dict(
    version_id="step-100",
    parent_id=None,
    metadata_hash="sha256:" + "a" * 64,
    created_at="2026-10-25T00:00:00Z",
)

CDC = ChunkingPolicy.content_defined(min_bytes=1024, avg_bytes=4096, max_bytes=8192)

POLICIES = {
    "fixed_1024": ChunkingPolicy.fixed(1024),
    "cdc": CDC,
    "file": ChunkingPolicy.file(),
}


def _data() -> dict[str, bytes]:
    rng = random.Random(20261025)
    return {
        "rank0/a.bin": rng.randbytes(200_000),
        "rank0/b.bin": rng.randbytes(97_333),
        "rank0/c.bin": b"",
    }


def _changed() -> dict[str, bytes]:
    rng = random.Random(991)
    data = _data()
    data["rank0/b.bin"] = rng.randbytes(97_333)
    return data


def _stage(root: Path, mapping: dict[str, bytes]) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    for name, payload in mapping.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        staged[name] = path
    return staged


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_golden_fixture_is_populated(golden) -> None:
    assert set(golden) == {"fixed_1024", "cdc", "file", "reuse_v1", "reuse_v2"}
    # 61 chunks prove the data exercises real content-defined cuts.
    assert len(golden["cdc"]["chunks"]) == 61
    assert len(golden["fixed_1024"]["chunks"]) == 293


@pytest.mark.parametrize("tag", ["fixed_1024", "cdc", "file"])
def test_native_manifest_matches_golden(tmp_path, golden, tag) -> None:
    store = ChunkStore(tmp_path / "cas")
    manifest = build_manifest_from_paths(
        files=_stage(tmp_path / "src", _data()),
        chunk_store=store,
        chunking=POLICIES[tag],
        hardlink=True,
        **META,
    )
    assert manifest.to_dict() == golden[tag]


def test_native_reuse_delta_matches_golden(tmp_path, golden) -> None:
    store = ChunkStore(tmp_path / "cas")
    first = build_manifest_from_paths(
        files=_stage(tmp_path / "src-1", _data()),
        chunk_store=store,
        chunking=CDC,
        hardlink=True,
        **META,
    )
    assert first.to_dict() == golden["reuse_v1"]
    previous_by_file: dict[str, list] = {}
    for chunk in first.chunks:
        previous_by_file.setdefault(chunk.file, []).append(chunk)
    second = build_manifest_from_paths(
        files=_stage(tmp_path / "src-2", _changed()),
        chunk_store=store,
        chunking=CDC,
        hardlink=True,
        reuse_chunks={
            name: tuple(chunks)
            for name, chunks in previous_by_file.items()
            if name != "rank0/b.bin"
        },
        version_id="step-200",
        parent_id="step-100",
        metadata_hash="sha256:" + "b" * 64,
        created_at="2026-10-25T01:00:00Z",
    )
    assert second.to_dict() == golden["reuse_v2"]
