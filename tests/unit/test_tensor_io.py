from __future__ import annotations

import numpy as np

from aevrel.planning.tensor_io import (
    _default_run_batch_bytes,
    byte_runs,
    coalesce_runs,
    paired_run_batches,
    reduce_bytes,
)


def test_gpu_codec_uses_larger_default_run_batch(monkeypatch) -> None:
    monkeypatch.delenv("AEVREL_RUN_BATCH_BYTES", raising=False)
    monkeypatch.setenv("AEVREL_GPU_CODEC", "nvcomp-gdeflate")
    assert _default_run_batch_bytes() == 16 * 1024 * 1024

    monkeypatch.setenv("AEVREL_RUN_BATCH_BYTES", str(8 * 1024 * 1024))
    assert _default_run_batch_bytes() == 8 * 1024 * 1024


def test_byte_runs_cover_rectangular_region() -> None:
    assert byte_runs((4, 4), (0, 0), (3, 2), 4) == [
        (0, 8),
        (16, 8),
        (32, 8),
    ]


def test_byte_runs_handle_last_dimension() -> None:
    assert byte_runs((2, 3), (1, 1), (1, 2), 4) == [(16, 8)]


def test_reduce_sum_preserves_dtype() -> None:
    left = np.array([1, 2, 3], dtype=np.float32)
    right = np.array([4, 5, 6], dtype=np.float32)
    result = reduce_bytes(
        [left.tobytes(), right.tobytes()], "sum", "float32"
    )
    assert np.frombuffer(result, dtype=np.float32).tolist() == [5, 7, 9]


def test_coalesce_runs_merges_contiguous_ranges_with_cap() -> None:
    assert coalesce_runs([(0, 4), (4, 4), (8, 4)], max_bytes=8) == [
        (0, 8),
        (8, 4),
    ]
    assert coalesce_runs([(0, 4), (8, 4)]) == [(0, 4), (8, 4)]


def test_paired_run_batches_requires_both_sides_contiguous() -> None:
    batches = paired_run_batches(
        [(0, 4), (4, 4), (8, 4)],
        [[(10, 4), (14, 4), (18, 4)]],
        max_bytes=8,
    )
    assert batches == [((0, 8), [(10, 8)]), ((8, 4), [(18, 4)])]


def test_paired_run_batches_does_not_cross_noncontiguous_target() -> None:
    batches = paired_run_batches(
        [(0, 4), (8, 4)],
        [[(0, 4), (4, 4)]],
    )
    assert batches == [((0, 4), [(0, 4)]), ((8, 4), [(4, 4)])]
