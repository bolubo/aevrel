"""In-process bindings to the native hot kernels.

One crate in ``native/aevrel-fast`` builds two artefacts: the ``aevrel-fast``
CLI (content-defined chunking plus content-addressed storage) and the
``aevrel_fast`` CPython extension module that this file loads. The extension is
an ``abi3-py39`` build, so a single artefact serves every interpreter from
CPython 3.9 on and no per-version builds are needed. It is loaded straight from
the cargo output directory by path, which keeps ``maturin`` and wheel
packaging out of the build.

There is deliberately **no Python fallback**. Every kernel behind this module
used to have a Python implementation; keeping one around "just in case" is how
two implementations of the same checksum start to disagree, and a checksum
disagreement is a silent data-integrity bug. A missing extension is therefore a
hard failure naming the build command, not a quiet downgrade to interpreted
code.
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from os import environ
from pathlib import Path
from types import ModuleType
from typing import Iterable, Sequence


EXTENSION_NAME = "aevrel_fast"

BUILD_HINT = (
    "the aevrel_fast native extension is not built; run `cargo build --release` "
    "in native/aevrel-fast, or point AEVREL_FAST_LIB at an existing "
    "libaevrel_fast.so"
)

# `cargo` writes a cdylib as `lib<name>.so` on Linux, `lib<name>.dylib` on macOS
# and `<name>.dll` on Windows. The bare `aevrel_fast.so` spelling appears when
# the artefact is installed or renamed by hand.
_ARTEFACT_NAMES = (
    "libaevrel_fast.so",
    "libaevrel_fast.dylib",
    "aevrel_fast.dll",
    "aevrel_fast.so",
)


class KernelUnavailable(ImportError):
    """Raised when the native kernel extension cannot be found or loaded."""


def release_directory() -> Path:
    """Directory cargo writes release artefacts into."""
    return (
        Path(__file__).resolve().parents[2]
        / "native"
        / "aevrel-fast"
        / "target"
        / "release"
    )


def extension_path() -> Path | None:
    """Locate the extension module, or ``None`` when it has not been built."""
    configured = environ.get("AEVREL_FAST_LIB")
    if configured:
        candidate = Path(configured)
        if not candidate.is_file():
            raise KernelUnavailable(f"AEVREL_FAST_LIB does not exist: {candidate}")
        return candidate
    directory = release_directory()
    for name in _ARTEFACT_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def available() -> bool:
    """Whether the extension module can be located."""
    try:
        return extension_path() is not None
    except KernelUnavailable:
        return False


@lru_cache(maxsize=1)
def extension() -> ModuleType:
    """Import the extension module, raising rather than falling back."""
    path = extension_path()
    if path is None:
        raise KernelUnavailable(BUILD_HINT)
    spec = importlib.util.spec_from_file_location(EXTENSION_NAME, path)
    if spec is None or spec.loader is None:
        raise KernelUnavailable(f"{path} is not a loadable extension module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        raise KernelUnavailable(f"cannot load {path}: {exc}") from exc
    return module


def byte_runs(
    local_shape: Sequence[int],
    local_offset: Sequence[int],
    region_size: Sequence[int],
    item_size: int,
) -> list[tuple[int, int]]:
    """Byte runs covering one region of a row-major local tensor."""
    return extension().byte_runs(
        list(local_shape), list(local_offset), list(region_size), item_size
    )


def coalesce_runs(
    runs: Iterable[tuple[int, int]],
    max_bytes: int,
) -> list[tuple[int, int]]:
    """Merge physically adjacent runs up to `max_bytes`."""
    return extension().coalesce_runs(list(runs), max_bytes)


def split_runs(runs: Iterable[tuple[int, int]], max_bytes: int) -> list[tuple[int, int]]:
    """Split every run so none exceeds `max_bytes`."""
    return extension().split_runs(list(runs), max_bytes)


def paired_run_batches(
    target_runs: Iterable[tuple[int, int]],
    source_run_lists: Iterable[Iterable[tuple[int, int]]],
    max_bytes: int,
) -> list[tuple[tuple[int, int], list[tuple[int, int]]]]:
    """Pair target runs with source runs, coalescing both sides together."""
    sources = [list(runs) for runs in source_run_lists]
    return extension().paired_run_batches(list(target_runs), sources, max_bytes)


def reduce_bytes(inputs: Iterable[bytes], op: str, dtype: str) -> bytes:
    """Reduce a stack of equally sized buffers element-wise."""
    return extension().reduce_bytes(list(inputs), op, dtype)


def verify_batches(
    target: str,
    sources: Sequence[str],
    batches: Iterable[tuple[int, int, Iterable[tuple[int, int]]]],
    op: str,
    dtype: str,
) -> int | None:
    """Verify target bytes against their sources in one native scan.

    ``batches`` is ``[(target_offset, target_size, [(source_offset, source_size)])]``
    in plan order. Returns the absolute target offset of the first mismatch, or
    ``None`` when every batch matches. The GIL is released for the whole scan.
    """
    return extension().verify_batches(
        target,
        list(sources),
        [
            (int(target_offset), int(target_size), [(int(o), int(s)) for o, s in inputs])
            for target_offset, target_size, inputs in batches
        ],
        op,
        dtype,
    )


def digest_bytes(data: bytes) -> str:
    """``sha256:<hex>`` for an in-memory payload."""
    return extension().digest_bytes(data)


def merkle_root(digests: Iterable[str]) -> str:
    """Fold ``sha256:<hex>`` digests into one Merkle root."""
    return extension().merkle_root(list(digests))


def sha256_file(path: str | Path) -> str:
    """Stream-hash a file in 1 MiB blocks."""
    return extension().sha256_file(str(path))


def sha256_bytes(data: bytes) -> str:
    """``sha256:<hex>`` for an in-memory payload."""
    return extension().sha256_bytes(data)


def sha256_concat(chunks: Iterable[bytes]) -> str:
    """Hash a sequence of payloads as though they were concatenated."""
    return extension().sha256_concat(list(chunks))


__all__ = [
    "BUILD_HINT",
    "EXTENSION_NAME",
    "KernelUnavailable",
    "available",
    "byte_runs",
    "coalesce_runs",
    "digest_bytes",
    "extension",
    "extension_path",
    "merkle_root",
    "paired_run_batches",
    "reduce_bytes",
    "release_directory",
    "sha256_bytes",
    "sha256_concat",
    "sha256_file",
    "verify_batches",
    "split_runs",
]
