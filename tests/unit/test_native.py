from __future__ import annotations

import pytest

from shardferry.cdp import (
    ChunkStore,
    ChunkingPolicy,
    build_manifest_from_paths,
    materialize_manifest,
)
from shardferry.native import available, build_manifest_native


def test_native_file_manifest_matches_python(tmp_path) -> None:
    if not available():
        pytest.skip("shardferry-fast release binary is not built")
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"native-data")
    (source / "b.bin").write_bytes(b"second-file")
    python_store = ChunkStore(tmp_path / "python-target")
    native_store = ChunkStore(tmp_path / "native-target")
    files = {
        "rank0/a.bin": source / "a.bin",
        "rank1/b.bin": source / "b.bin",
    }
    python_manifest = build_manifest_from_paths(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files=files,
        chunk_store=python_store,
        hardlink=True,
    )
    native_manifest = build_manifest_native(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files=files,
        chunk_store=native_store,
    )
    assert native_manifest == python_manifest
    assert native_manifest.chunks


def test_native_cdc_manifest_materializes(tmp_path) -> None:
    if not available():
        pytest.skip("shardferry-fast release binary is not built")
    source = tmp_path / "source.bin"
    data = bytes(range(256)) * 256
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "native-target")
    policy = ChunkingPolicy.content_defined(
        min_bytes=1024,
        avg_bytes=4096,
        max_bytes=8192,
    )
    manifest = build_manifest_native(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": source},
        chunk_store=store,
        chunking=policy,
    )
    assert len(manifest.chunks) > 1
    output = tmp_path / "output"
    materialize_manifest(manifest, store, output)
    assert (output / "a.bin").read_bytes() == data
