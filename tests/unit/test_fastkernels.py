from __future__ import annotations

import hashlib
import inspect

import pytest

from aevrel import fastkernels
from aevrel.fastkernels import (
    BUILD_HINT,
    KernelUnavailable,
    available,
    extension,
)
from aevrel.planning import tensor_io
from aevrel.planning.tensor_io import TensorIOError, byte_runs, reduce_bytes


KERNELS = (
    "byte_runs",
    "coalesce_runs",
    "split_runs",
    "paired_run_batches",
    "reduce_bytes",
    "digest_bytes",
    "merkle_root",
    "sha256_file",
    "sha256_bytes",
    "sha256_concat",
)


def half(*bits: int) -> bytes:
    return b"".join(int(value).to_bytes(2, "little") for value in bits)


def test_extension_is_built() -> None:
    """The native kernels are the only implementation.

    A missing extension is a hard error everywhere, so this suite must not
    silently skip when the artefact has not been built.
    """
    assert available(), BUILD_HINT


def test_extension_exposes_every_kernel() -> None:
    module = extension()
    for name in KERNELS:
        assert callable(getattr(module, name)), name


def test_missing_artefact_names_the_build_command(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AEVREL_FAST_LIB", raising=False)
    monkeypatch.setattr(fastkernels, "release_directory", lambda: tmp_path)
    extension.cache_clear()
    try:
        assert not available()
        with pytest.raises(KernelUnavailable) as caught:
            extension()
        assert str(caught.value) == BUILD_HINT
    finally:
        extension.cache_clear()


def test_override_pointing_nowhere_is_an_error(monkeypatch) -> None:
    monkeypatch.setenv("AEVREL_FAST_LIB", "/nonexistent/libaevrel_fast.so")
    try:
        with pytest.raises(KernelUnavailable, match="AEVREL_FAST_LIB does not exist"):
            fastkernels.extension_path()
    finally:
        monkeypatch.delenv("AEVREL_FAST_LIB", raising=False)


def test_kernels_keep_the_retired_python_messages() -> None:
    with pytest.raises(TensorIOError, match="tensor region rank mismatch"):
        byte_runs((4, 8), (0,), (1, 8), 4)
    with pytest.raises(TensorIOError, match="unsupported dtype: not-a-dtype"):
        reduce_bytes([b"\x00" * 4], "sum", "not-a-dtype")


def test_tensor_io_delegates_instead_of_reimplementing() -> None:
    """The retired numpy kernels must not linger as a fallback."""
    source = inspect.getsource(tensor_io)
    assert "fastkernels" in source
    assert "numpy" not in source


def test_float16_sum_resolves_nan_by_output_width() -> None:
    """float16 `sum` picks the NaN that wins based on how many outputs it has.

    numpy's pairwise fast path (one output element) keeps the earliest NaN,
    its element-wise loop (wider output) the latest. Both shapes are wired
    through untouched, so flattening the distinction would fail here.
    """
    # Three numbers followed by two distinct quiet NaNs, 0x7f26 then 0x7e29.
    values = (0xC7F0, 0x980F, 0xB62D, 0x7F26, 0x7E29)
    narrow = [half(value) for value in values]
    assert reduce_bytes(narrow, "sum", "float16") == half(0x7F26)

    wide = [half(*([value] * 1024)) for value in values]
    assert reduce_bytes(wide, "sum", "float16") == half(*([0x7E29] * 1024))


def test_native_hashes_agree_with_hashlib() -> None:
    """`hashlib` and the native kernels both stay live, so pin them together."""
    for size in (0, 1, 64, 4096, 1024 * 1024):
        payload = (bytes(range(256)) * (size // 256 + 1))[:size]
        expected = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert fastkernels.digest_bytes(payload) == expected, size
        assert fastkernels.sha256_bytes(payload) == expected, size

    chunks = [b"alpha", b"", b"beta"]
    assert fastkernels.sha256_concat(chunks) == (
        "sha256:" + hashlib.sha256(b"".join(chunks)).hexdigest()
    )


def test_sha256_file_matches_hashlib(tmp_path) -> None:
    payload = b"checkpoint bytes" * 10000
    path = tmp_path / "tensor.bin"
    path.write_bytes(payload)
    assert fastkernels.sha256_file(path) == (
        "sha256:" + hashlib.sha256(payload).hexdigest()
    )
