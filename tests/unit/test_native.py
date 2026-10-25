from __future__ import annotations

import pytest

from aevrel.cdp import (
    CDPError,
    ChunkStore,
    ChunkingPolicy,
    materialize_manifest,
)
from aevrel.native import (
    BUILD_HINT,
    available,
    build_manifest_native,
)


def test_native_binary_is_built() -> None:
    """The native chunker is the only chunking implementation.

    A missing release binary is a hard error everywhere, so this suite must not
    silently skip when the binary is absent.
    """
    assert available(), BUILD_HINT


def test_native_file_manifest_materializes(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"native-data")
    (source / "b.bin").write_bytes(b"second-file")
    store = ChunkStore(tmp_path / "native-target")
    manifest = build_manifest_native(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={
            "rank0/a.bin": source / "a.bin",
            "rank1/b.bin": source / "b.bin",
        },
        chunk_store=store,
    )
    assert manifest.chunks
    output = tmp_path / "output"
    materialize_manifest(manifest, store, output)
    assert (output / "rank0" / "a.bin").read_bytes() == b"native-data"
    assert (output / "rank1" / "b.bin").read_bytes() == b"second-file"


def test_native_cdc_manifest_materializes(tmp_path) -> None:
    source = tmp_path / "source.bin"
    data = bytes(range(256)) * 256
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "native-target")
    manifest = build_manifest_native(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": source},
        chunk_store=store,
        chunking=ChunkingPolicy.content_defined(
            min_bytes=1024,
            avg_bytes=4096,
            max_bytes=8192,
        ),
    )
    assert len(manifest.chunks) > 1
    output = tmp_path / "output"
    materialize_manifest(manifest, store, output)
    assert (output / "a.bin").read_bytes() == data


def test_native_fixed_manifest_materializes(tmp_path) -> None:
    source = tmp_path / "source.bin"
    data = bytes(range(256)) * 64
    source.write_bytes(data)
    store = ChunkStore(tmp_path / "native-target")
    manifest = build_manifest_native(
        version_id="step-100",
        parent_id=None,
        metadata_hash="sha256:" + "a" * 64,
        created_at="2026-10-22T00:00:00Z",
        files={"a.bin": source},
        chunk_store=store,
        chunking=ChunkingPolicy.fixed(4096),
    )
    assert len(manifest.chunks) == (len(data) + 4095) // 4096
    output = tmp_path / "output"
    materialize_manifest(manifest, store, output)
    assert (output / "a.bin").read_bytes() == data


def test_missing_native_binary_is_a_hard_error(tmp_path, monkeypatch) -> None:
    """No silent fallback: an unavailable binary must fail loudly."""
    monkeypatch.setattr("aevrel.native.default_binary", lambda: None)
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    store = ChunkStore(tmp_path / "target")
    with pytest.raises(CDPError) as excinfo:
        build_manifest_native(
            version_id="step-100",
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            files={"a.bin": source},
            chunk_store=store,
            chunking=ChunkingPolicy.file(),
        )
    assert "cargo build --release" in str(excinfo.value)
