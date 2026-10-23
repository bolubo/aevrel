from __future__ import annotations

import math

import numpy as np
import pytest

from aevrel.lrs.layout import (
    LayoutError,
    Partial,
    Replicate,
    Shard,
    TensorLayout,
    byte_ranges,
    execute_plan,
    iter_shards,
    plan_transfer,
)


def _full(shape: tuple[int, ...]) -> np.ndarray:
    return np.arange(
        1,
        math.prod(shape) + 1,
        dtype=np.float64,
    ).reshape(shape)


def _materialize(
    full: np.ndarray,
    layout: TensorLayout,
) -> dict[tuple[int, ...], np.ndarray]:
    result = {}
    for coord, shard in iter_shards(layout).items():
        region = tuple(
            slice(offset, offset + size)
            for offset, size in zip(shard.offsets, shard.sizes)
        )
        result[coord] = full[region]
    return result


def _check(plan, full: np.ndarray) -> None:
    source_locals = _materialize(full, plan.source)
    result = execute_plan(plan, source_locals)
    expected = _materialize(full, plan.target)
    assert set(result) == set(expected)
    for coord in expected:
        np.testing.assert_array_equal(result[coord], expected[coord])


def test_1d_shard_2_to_4() -> None:
    full = _full((8,))
    source = TensorLayout((8,), (2,), (Shard(0),))
    target = TensorLayout((8,), (4,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)
    assert plan.total_bytes(8) == 64


def test_2d_mesh_replicate_and_shard() -> None:
    full = _full((8, 4))
    source = TensorLayout(
        (8, 4),
        (2, 2),
        (Replicate(), Shard(0)),
        ("dp", "tp"),
    )
    target = TensorLayout((8, 4), (4,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)


def test_2d_shard_both_dims() -> None:
    full = _full((8, 4))
    source = TensorLayout(
        (8, 4),
        (2, 2),
        (Shard(0), Shard(1)),
    )
    target = TensorLayout((8, 4), (4,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)


def test_shard_to_replicate() -> None:
    full = _full((8,))
    source = TensorLayout((8,), (2,), (Shard(0),))
    target = TensorLayout((8,), (2,), (Replicate(),))
    plan = plan_transfer(source, target)
    _check(plan, full)
    assert plan.total_bytes(8) == 128
    assert plan.target_bytes(8) == 128


def test_replicate_to_shard() -> None:
    full = _full((8,))
    source = TensorLayout((8,), (2,), (Replicate(),))
    target = TensorLayout((8,), (2,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)
    assert plan.total_bytes(8) == 64


def test_partial_sum() -> None:
    full = _full((8,))
    source = TensorLayout((8,), (2,), (Partial(),))
    target = TensorLayout((8,), (2,), (Shard(0),))
    plan = plan_transfer(source, target)
    source_locals = {
        coord: full / 2
        for coord in iter_shards(source)
    }
    result = execute_plan(plan, source_locals)
    expected = _materialize(full, target)
    for coord in expected:
        np.testing.assert_array_equal(result[coord], expected[coord])
    assert all(copy.reduce for copy in plan.copies)


def test_partial_with_shard() -> None:
    full = _full((8,))
    source = TensorLayout(
        (8,),
        (2, 2),
        (Shard(0), Partial()),
    )
    target = TensorLayout((8,), (2,), (Shard(0),))
    plan = plan_transfer(source, target)
    source_shards = iter_shards(source)
    source_locals = {}
    for coord, shard in source_shards.items():
        region = slice(shard.offsets[0], shard.offsets[0] + shard.sizes[0])
        source_locals[coord] = full[region] / 2
    result = execute_plan(plan, source_locals)
    expected = _materialize(full, target)
    for coord in expected:
        np.testing.assert_array_equal(result[coord], expected[coord])


def test_uneven_shard() -> None:
    full = _full((10,))
    source = TensorLayout((10,), (4,), (Shard(0),))
    target = TensorLayout((10,), (3,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)
    assert plan.target_bytes(8) == 80


def test_nested_shard_same_dim() -> None:
    full = _full((12,))
    source = TensorLayout(
        (12,),
        (2, 3),
        (Shard(0), Shard(0)),
    )
    target = TensorLayout((12,), (6,), (Shard(0),))
    plan = plan_transfer(source, target)
    _check(plan, full)


def test_m5_tp_layouts() -> None:
    w1 = _full((4, 4))
    source_w1 = TensorLayout((4, 4), (2,), (Shard(1),))
    target_w1 = TensorLayout((4, 4), (4,), (Shard(1),))
    _check(plan_transfer(source_w1, target_w1), w1)

    w2 = _full((4, 1))
    source_w2 = TensorLayout((4, 1), (2,), (Shard(0),))
    target_w2 = TensorLayout((4, 1), (4,), (Shard(0),))
    _check(plan_transfer(source_w2, target_w2), w2)


def test_byte_ranges_2d() -> None:
    assert byte_ranges((4, 5), (1, 2), (2, 2), 4) == (
        (28, 8),
        (48, 8),
    )
    assert byte_ranges((4, 5), (0, 0), (2, 5), 4) == (
        (0, 40),
    )
    assert byte_ranges((8,), (2,), (3,), 8) == ((16, 24),)


def test_invalid_layouts() -> None:
    with pytest.raises(LayoutError):
        TensorLayout((8,), (2,), (Shard(1),))
    with pytest.raises(LayoutError):
        TensorLayout((8,), (2, 2), (Shard(0),))
    with pytest.raises(LayoutError):
        plan_transfer(
            TensorLayout((8,), (2,), (Shard(0),)),
            TensorLayout((9,), (2,), (Shard(0),)),
        )
