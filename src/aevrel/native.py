from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from .cdp import (
    CDPError,
    ChunkRef,
    ChunkStore,
    ChunkingPolicy,
    VersionManifest,
    _validate_relative_file,
    manifest_root,
)


BUILD_HINT = (
    "aevrel-fast release binary is not built; run "
    "`cargo build --release` in native/aevrel-fast"
)


def default_binary() -> Path | None:
    configured = os.environ.get("AEVREL_FAST")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(
        repo_root / "native" / "aevrel-fast" / "target" / "release" /
        "aevrel-fast"
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def available() -> bool:
    return default_binary() is not None


def build_manifest_native(
    *,
    version_id: str,
    parent_id: str | None,
    metadata_hash: str,
    created_at: str,
    files: Mapping[str, Path],
    chunk_store: ChunkStore,
    binary: str | Path | None = None,
    chunking: ChunkingPolicy | None = None,
    hardlink: bool = True,
) -> VersionManifest:
    """Ingest target-layout files through the native aevrel-fast binary.

    This is the single chunking implementation: the binary performs content
    -defined or fixed chunking, SHA-256 hashing and content-addressed storage.
    There is deliberately no Python fallback; a missing binary is a hard error.
    """
    executable = Path(binary) if binary is not None else default_binary()
    if executable is None:
        raise CDPError(BUILD_HINT)
    entries = []
    for file_name, path in sorted(files.items()):
        _validate_relative_file(file_name)
        source = Path(path).resolve()
        if not source.is_file():
            raise CDPError(f"target-layout file not found: {source}")
        entries.append({"file": file_name, "path": str(source)})
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="aevrel-fast-",
        encoding="utf-8",
        delete=False,
    ) as stream:
        json.dump(entries, stream)
        map_path = Path(stream.name)
    try:
        command = [
            str(executable),
            "--cas",
            str(chunk_store.cas),
            "--file-map",
            str(map_path),
        ]
        if chunking is not None:
            if chunking.algorithm == "file":
                command.extend(["--chunk-alg", "file"])
            elif chunking.algorithm == "fixed":
                command.extend(
                    ["--chunk-alg", "fixed", "--chunk-bytes", str(chunking.chunk_bytes)]
                )
            elif chunking.algorithm == "cdc":
                command.extend(
                    [
                        "--chunk-alg",
                        "cdc",
                        "--min-bytes",
                        str(chunking.min_bytes),
                        "--avg-bytes",
                        str(chunking.avg_bytes),
                        "--max-bytes",
                        str(chunking.max_bytes),
                    ]
                )
            else:
                raise CDPError(
                    f"native ingest does not support {chunking.algorithm}"
                )
        if not hardlink:
            command.append("--copy")
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        try:
            map_path.unlink()
        except OSError:
            pass
    if completed.returncode != 0:
        raise CDPError(
            "aevrel-fast failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        payload = json.loads(completed.stdout)
        chunks = tuple(
            sorted(
                (ChunkRef.from_dict(item) for item in payload["chunks"]),
                key=lambda chunk: (
                    chunk.file,
                    chunk.offset,
                    chunk.length,
                    chunk.digest,
                ),
            )
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CDPError(f"invalid aevrel-fast output: {exc}") from exc
    return VersionManifest(
        version="1",
        version_id=version_id,
        parent_id=parent_id,
        metadata_hash=metadata_hash,
        created_at=created_at,
        root=manifest_root(
            version_id=version_id,
            parent_id=parent_id,
            metadata_hash=metadata_hash,
            created_at=created_at,
            chunks=chunks,
        ),
        chunks=chunks,
    )


def native_binary_or_raise() -> Path:
    executable = default_binary()
    if executable is None:
        raise CDPError(BUILD_HINT)
    return executable


__all__ = [
    "available",
    "build_manifest_native",
    "default_binary",
    "native_binary_or_raise",
    "BUILD_HINT",
]
