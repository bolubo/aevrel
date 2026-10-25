#!/usr/bin/env python3
"""Capture golden baselines for the R2 native kernels.

Snapshots the *pre-refactor* Python implementations of the byte-range,
reduction, Merkle and checksum kernels so the Rust replacements can be proven
byte-for-byte equivalent. The output is tests/fixtures/golden_kernels.json.

This is a one-shot provenance tool, not something CI reruns. The reference
implementations it captures have since been deleted, so running it against the
current tree would record the native kernels as their own baseline — worthless
as evidence and actively misleading. Recover the Python modules from git first:

    mkdir -p /tmp/reference/src/aevrel/{planning,verification}
    for path in planning/tensor_io.py verification/proof.py; do
        git show REFERENCE_COMMIT:src/aevrel/$path > /tmp/reference/src/aevrel/$path
    done

    python scripts/capture_kernel_golden.py
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import random
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np

from aevrel.planning.tensor_io import (  # noqa: E402
    TensorIOError,
    byte_runs,
    coalesce_runs,
    paired_run_batches,
    reduce_bytes,
)
from aevrel.verification.proof import _digest_bytes, _merkle_root  # noqa: E402

SEED = 20261025


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _prng_bytes(seed: int, size: int) -> bytes:
    """Deterministic pseudo-random bytes (xorshift64*).

    Implemented identically in the Rust golden test so both sides can
    regenerate the same payloads from (seed, size) alone; `random.Random`
    output is not portable across languages.
    """
    state = seed & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while len(out) < size:
        state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
        state ^= state >> 7
        state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
        out.extend(state.to_bytes(8, "little"))
    return bytes(out[:size])


def _random_bytes(seed: int, size: int) -> bytes:
    return _prng_bytes(seed, size)


def _capture(fn, *args, **kwargs):
    """Record either the result or the exact error message."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return {"result": fn(*args, **kwargs)}
    except Exception as exc:  # noqa: BLE001 - golden fixture records the message
        return {"error": f"{type(exc).__name__}: {exc}"}


def _capture_bytes(fn, *args, **kwargs):
    """Same as _capture but base64-encodes a bytes result."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return {"result_b64": _b64(fn(*args, **kwargs))}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def byte_run_cases() -> list[dict]:
    cases = [
        ("rectangular-2d", (4, 4), (0, 0), (3, 2), 4),
        ("offset-2d", (4, 4), (1, 1), (2, 2), 2),
        ("one-dimensional", (8,), (2,), (3,), 4),
        ("full-3d", (2, 3, 4), (0, 0, 0), (2, 3, 4), 1),
        ("partial-last-dim", (2, 3, 4), (0, 0, 1), (2, 3, 2), 4),
        ("full-2d-item8", (5, 7), (0, 0), (5, 7), 8),
        ("strided-3d", (3, 3, 3), (1, 1, 1), (2, 2, 2), 2),
        ("empty-region", (6,), (0,), (0,), 4),
        ("zero-last-dim", (2, 2), (0, 0), (2, 0), 4),
        ("rank-mismatch", (2, 2), (0,), (2, 2), 4),
        ("out-of-bounds", (2, 2), (1, 0), (2, 2), 4),
        ("negative-offset", (2, 2), (-1, 0), (1, 1), 4),
        ("wide-rows", (64, 128), (0, 0), (64, 128), 2),
    ]
    payload = []
    for name, shape, offset, region, item_size in cases:
        entry = {
            "name": name,
            "shape": list(shape),
            "offset": list(offset),
            "region": list(region),
            "item_size": item_size,
        }
        entry.update(_capture(byte_runs, shape, offset, region, item_size))
        payload.append(entry)
    return payload


def coalesce_cases() -> list[dict]:
    cases = [
        ("adjacent-default", [(0, 4), (4, 4), (8, 4)], None),
        ("adjacent-max8", [(0, 4), (4, 4), (8, 4)], 8),
        ("gap", [(0, 4), (100, 4), (104, 4)], None),
        ("empty", [], None),
        ("zero-length-middle", [(0, 4), (4, 0), (4, 4)], None),
        ("duplicate-offsets", [(5, 3), (5, 3)], None),
        ("max-zero", [(0, 4), (4, 4), (8, 4)], 0),
        ("mixed", [(0, 4), (4, 4), (9, 1), (10, 6), (16, 16), (32, 1)], 12),
        ("single", [(7, 3)], None),
        ("backwards", [(10, 4), (4, 4)], None),
    ]
    payload = []
    for name, runs, max_bytes in cases:
        entry = {"name": name, "runs": [list(run) for run in runs]}
        if max_bytes is None:
            entry.update(_capture(coalesce_runs, runs))
        else:
            entry["max_bytes"] = max_bytes
            entry.update(_capture(coalesce_runs, runs, max_bytes))
        payload.append(entry)
    return payload


def split_cases() -> list[dict]:
    from aevrel.planning.tensor_io import _split_runs

    cases = [
        ("plain", [(0, 10), (10, 10), (20, 10)], 16),
        ("exact", [(0, 8), (8, 8)], 8),
        ("single-huge", [(0, 100)], 7),
        ("empty", [], 16),
        ("zero-length", [(0, 0), (4, 4)], 8),
        ("invalid-max", [(0, 4)], 0),
    ]
    payload = []
    for name, runs, max_bytes in cases:
        entry = {
            "name": name,
            "runs": [list(run) for run in runs],
            "max_bytes": max_bytes,
        }
        entry.update(_capture(_split_runs, runs, max_bytes))
        payload.append(entry)
    return payload


def paired_cases() -> list[dict]:
    cases = [
        (
            "single-source-adjacent",
            [(0, 10), (10, 10), (20, 10)],
            [[(0, 10), (10, 10), (20, 10)]],
            None,
        ),
        (
            "split-max16",
            [(0, 10), (10, 10), (20, 10)],
            [[(0, 10), (10, 10), (20, 10)]],
            16,
        ),
        (
            "two-sources-aligned",
            [(0, 12), (12, 12)],
            [[(0, 12), (12, 12)], [(100, 12), (112, 12)]],
            None,
        ),
        (
            "two-sources-unaligned",
            [(0, 12), (12, 12)],
            [[(0, 12), (12, 12)], [(100, 6), (106, 6), (112, 12)]],
            None,
        ),
        (
            "count-mismatch",
            [(0, 10), (10, 10)],
            [[(0, 10), (10, 10)], [(0, 20)]],
            None,
        ),
        ("empty-target", [], [[]], None),
        (
            "source-gap-breaks-batch",
            [(0, 8), (8, 8), (16, 8)],
            [[(0, 8), (8, 8), (16, 8)], [(0, 8), (20, 8), (28, 8)]],
            None,
        ),
        (
            "max-bytes-boundary",
            [(0, 6), (6, 6), (12, 6)],
            [[(0, 6), (6, 6), (12, 6)]],
            12,
        ),
    ]
    payload = []
    for name, target, sources, max_bytes in cases:
        entry = {
            "name": name,
            "target": [list(run) for run in target],
            "sources": [[list(run) for run in runs] for runs in sources],
        }
        if max_bytes is None:
            entry.update(_capture(paired_run_batches, target, sources))
        else:
            entry["max_bytes"] = max_bytes
            entry.update(_capture(paired_run_batches, target, sources, max_bytes))
        payload.append(entry)
    return payload


def reduce_cases() -> list[dict]:
    dtypes = ["float32", "float16", "float64", "int64", "int32", "uint8"]
    ops = ["copy", "sum", "avg", "max", "min"]
    payload = []
    for dtype in dtypes:
        for op in ops:
            for count, size in ((1, 64), (2, 64), (3, 17), (5, 1024)):
                inputs = [
                    _random_bytes(SEED + index * 7919 + size, size * _itemsize(dtype))
                    for index in range(count)
                ]
                payload.append(
                    {
                        "name": f"{dtype}-{op}-{count}x{size}",
                        "op": op,
                        "dtype": dtype,
                        "inputs_b64": [_b64(value) for value in inputs],
                        **_capture_bytes(reduce_bytes, inputs, op, dtype),
                    }
                )
    # NaN propagation for float reductions.
    nan_inputs = [
        b"\x00\x00\xc0\x7f" + b"\x00\x00\x80\x3f",
        b"\x00\x00\x00\x00" + b"\x00\x00\x80\x3f",
    ]
    for op in ("max", "min", "sum", "avg"):
        payload.append(
            {
                "name": f"float32-{op}-nan",
                "op": op,
                "dtype": "float32",
                "inputs_b64": [_b64(value) for value in nan_inputs],
                **_capture_bytes(reduce_bytes, nan_inputs, op, "float32"),
            }
        )
    # NaN propagation for half-precision reductions (1.0 == 0x3c00, NaN == 0x7e00).
    nan16_inputs = [
        b"\x00\x7e" + b"\x00\x3c",
        b"\x00\x00" + b"\x00\x3c",
    ]
    for op in ("max", "min", "sum", "avg"):
        payload.append(
            {
                "name": f"float16-{op}-nan",
                "op": op,
                "dtype": "float16",
                "inputs_b64": [_b64(value) for value in nan16_inputs],
                **_capture_bytes(reduce_bytes, nan16_inputs, op, "float16"),
            }
        )
    # float16 sum is the one reduction whose *shape* changes the answer. numpy
    # lowers a single-output reduction onto its pairwise float32 fast path and
    # everything wider onto the element-wise HALF loop, and the two disagree on
    # which NaN wins: the pairwise path keeps the earliest, the element-wise
    # path the latest. Cover both shapes with two distinct quiet NaNs so the
    # winner is observable, and probe the pairwise block knees (eight operands
    # switches to eight accumulators, above PW_BLOCKSIZE recurses).
    nan_order = [0xC7F0, 0x980F, 0xB62D, 0x7F26, 0x7E29]
    for length in (1, 2, 1024):
        nan_order_inputs = [
            np.array([value] * length, dtype=np.uint16).view(np.float16).tobytes()
            for value in nan_order
        ]
        payload.append(
            {
                "name": f"float16-sum-nan-order-len{length}",
                "op": "sum",
                "dtype": "float16",
                "inputs_b64": [_b64(value) for value in nan_order_inputs],
                **_capture_bytes(reduce_bytes, nan_order_inputs, "sum", "float16"),
            }
        )
    # Signaling NaNs are quieted on the way through float32 by every path.
    for length in (1, 2):
        snan_inputs = [
            np.array([value] * length, dtype=np.uint16).view(np.float16).tobytes()
            for value in (0x7C01, 0x3C00)
        ]
        payload.append(
            {
                "name": f"float16-sum-snan-first-len{length}",
                "op": "sum",
                "dtype": "float16",
                "inputs_b64": [_b64(value) for value in snan_inputs],
                **_capture_bytes(reduce_bytes, snan_inputs, "sum", "float16"),
            }
        )
        reversed_inputs = list(reversed(snan_inputs))
        payload.append(
            {
                "name": f"float16-sum-snan-last-len{length}",
                "op": "sum",
                "dtype": "float16",
                "inputs_b64": [_b64(value) for value in reversed_inputs],
                **_capture_bytes(reduce_bytes, reversed_inputs, "sum", "float16"),
            }
        )
    for count in (9, 130):
        block_inputs = [
            np.array([0x3C00 + index], dtype=np.uint16).view(np.float16).tobytes()
            for index in range(count)
        ]
        payload.append(
            {
                "name": f"float16-sum-pairwise-{count}x1",
                "op": "sum",
                "dtype": "float16",
                "inputs_b64": [_b64(value) for value in block_inputs],
                **_capture_bytes(reduce_bytes, block_inputs, "sum", "float16"),
            }
        )
    # Negative integer means truncate toward zero like ndarray.astype.
    signed = [
        np.array([-3, 3, -7], dtype="int32").tobytes(),
        np.array([2, -2, 2], dtype="int32").tobytes(),
    ]
    payload.append(
        {
            "name": "int32-avg-negative",
            "op": "avg",
            "dtype": "int32",
            "inputs_b64": [_b64(value) for value in signed],
            **_capture_bytes(reduce_bytes, signed, "avg", "int32"),
        }
    )
    # Integer overflow wraps like numpy.
    wrap = [b"\xff" * 8, b"\x02\x00\x00\x00\x00\x00\x00\x00"]
    payload.append(
        {
            "name": "int64-sum-overflow",
            "op": "sum",
            "dtype": "int64",
            "inputs_b64": [_b64(value) for value in wrap],
            **_capture_bytes(reduce_bytes, wrap, "sum", "int64"),
        }
    )
    # Error paths.
    errors = [
        ("bfloat16-sum", ["sum"], "bfloat16", [_random_bytes(1, 8)]),
        ("unknown-op", ["prod"], "float32", [_random_bytes(2, 16)]),
        ("empty-inputs", ["sum"], "float32", []),
        ("size-mismatch", ["sum"], "float32", [_random_bytes(3, 8), _random_bytes(4, 12)]),
        ("copy-two-inputs", ["copy"], "float32", [_random_bytes(5, 8), _random_bytes(6, 8)]),
        # A dtype numpy genuinely rejects: `float128` would be accepted on Linux
        # and would silently stop covering the unsupported-dtype error path.
        ("unknown-dtype", ["sum"], "not-a-dtype", [_random_bytes(7, 16)]),
        ("empty-single-input", ["sum"], "float32", [b""]),
    ]
    for name, args, dtype, inputs in errors:
        op = args[0]
        payload.append(
            {
                "name": name,
                "op": op,
                "dtype": dtype,
                "inputs_b64": [_b64(value) for value in inputs],
                **_capture_bytes(reduce_bytes, inputs, op, dtype),
            }
        )
    return payload


def _itemsize(dtype: str) -> int:
    sizes = {
        "float32": 4,
        "float16": 2,
        "float64": 8,
        "int64": 8,
        "int32": 4,
        "uint8": 1,
    }
    return sizes[dtype]


def merkle_cases() -> list[dict]:
    payload = []
    counts = [0, 1, 2, 3, 4, 5, 8, 17, 33]
    for count in counts:
        digests = [
            _digest_bytes(_random_bytes(SEED + index, 32)) for index in range(count)
        ]
        payload.append(
            {
                "name": f"merkle-{count}",
                "digests": digests,
                **_capture(_merkle_root, digests),
            }
        )
    # Unsorted input must be sorted internally before folding.
    digests = [
        _digest_bytes(_random_bytes(SEED + 100 + index, 32)) for index in range(6)
    ]
    payload.append(
        {
            "name": "merkle-unsorted",
            "digests": list(reversed(digests)),
            **_capture(_merkle_root, digests),
        }
    )
    return payload


def digest_cases() -> list[dict]:
    payload = []
    for name, data in (
        ("empty", b""),
        ("single-byte", b"\x00"),
        ("prefix", b"aevrel:leaf\0"),
        ("random-1k", _random_bytes(SEED, 1024)),
    ):
        payload.append(
            {"name": name, "data_b64": _b64(data), **_capture(_digest_bytes, data)}
        )
    return payload


def sha256_file_cases() -> list[dict]:
    """Deterministic pseudo-random content, regenerated identically in Rust."""
    payload = []
    for size in (0, 1, 1023, 65536, 1024 * 1024, 3 * 1024 * 1024 + 1):
        data = _random_bytes(SEED, size)
        payload.append(
            {
                "name": f"sha256-file-{size}",
                "seed": SEED,
                "size": size,
                "result": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    return payload


def sha256_bytes_cases() -> list[dict]:
    payload = []
    for size in (0, 1, 64, 4096, 100000):
        data = _random_bytes(SEED + 31, size)
        payload.append(
            {
                "name": f"sha256-bytes-{size}",
                "data_b64": _b64(data),
                "result": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    return payload


def sha256_concat_cases() -> list[dict]:
    """Chunked hashing must equal hashing the whole buffer."""
    payload = []
    for size, chunk in ((0, 1024), (5000, 1024), (100000, 4096), (70000, 65536)):
        data = _random_bytes(SEED + 57, size)
        payload.append(
            {
                "name": f"sha256-concat-{size}-chunk{chunk}",
                "data_b64": _b64(data),
                "chunk_bytes": chunk,
                "result": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    return payload


REFERENCE_COMMIT = "b46e7ad"

# Each reference implementation and a marker that only survives while the
# Python code is still the thing being captured.
_REFERENCE_MARKERS = {
    "planning.tensor_io.reduce_bytes": (reduce_bytes, "np.frombuffer"),
    "verification.proof._merkle_root": (_merkle_root, "bytes.fromhex"),
}


def _require_pre_refactor_sources() -> None:
    """Refuse to record the native kernels as their own baseline."""
    stale = [
        name
        for name, (function, marker) in _REFERENCE_MARKERS.items()
        if marker not in inspect.getsource(function)
    ]
    if not stale:
        return
    raise SystemExit(
        "refusing to capture: "
        + ", ".join(stale)
        + " no longer hold the Python reference implementation, so the fixture "
        "would record the native kernels as their own baseline — worthless as "
        "evidence. Recover the pre-refactor modules from git first: "
        f"REFERENCE_COMMIT={REFERENCE_COMMIT} (see this script's docstring)."
    )


def main() -> int:
    _require_pre_refactor_sources()
    fixture = {
        "version": 1,
        "seed": SEED,
        "cases": {
            "byte_runs": byte_run_cases(),
            "coalesce_runs": coalesce_cases(),
            "split_runs": split_cases(),
            "paired_run_batches": paired_cases(),
            "reduce_bytes": reduce_cases(),
            "merkle_root": merkle_cases(),
            "digest_bytes": digest_cases(),
            "sha256_file": sha256_file_cases(),
            "sha256_bytes": sha256_bytes_cases(),
            "sha256_concat": sha256_concat_cases(),
        },
    }
    destination = ROOT / "tests" / "fixtures" / "golden_kernels.json"
    destination.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    total = sum(len(cases) for cases in fixture["cases"].values())
    print(f"wrote {destination} ({total} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
