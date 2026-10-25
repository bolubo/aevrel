from __future__ import annotations

import itertools
import math
import random

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


def _reference_byte_ranges(
    shape: tuple[int, ...],
    offsets: tuple[int, ...],
    sizes: tuple[int, ...],
    item_size: int,
) -> tuple[tuple[int, int], ...]:
    """The pre-migration implementation, kept as an independent oracle.

    `byte_ranges` now delegates to the native kernel, so this is the only place
    the original stride walk survives. It exists to prove the delegation is
    value-identical; nothing in `src/` imports it.
    """
    ranges: list[tuple[int, int]] = []
    if any(size == 0 for size in sizes):
        return ()
    strides = [1] * len(shape)
    for index in range(len(shape) - 2, -1, -1):
        strides[index] = strides[index + 1] * shape[index + 1]
    if len(shape) == 1:
        return ((offsets[0] * item_size, sizes[0] * item_size),)
    outer = [
        range(offsets[index], offsets[index] + sizes[index])
        for index in range(len(shape) - 1)
    ]
    for outer_index in itertools.product(*outer):
        start = sum(
            outer_index[index] * strides[index] for index in range(len(shape) - 1)
        ) + offsets[-1]
        start_byte = start * item_size
        length_byte = sizes[-1] * item_size
        if ranges and ranges[-1][0] + ranges[-1][1] == start_byte:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + length_byte)
        else:
            ranges.append((start_byte, length_byte))
    return tuple(ranges)


def test_byte_ranges_matches_the_reference_implementation() -> None:
    """Randomised equivalence over every rank and item size in range."""
    generator = random.Random(20261025)
    for _ in range(400):
        rank = generator.randint(1, 4)
        shape = tuple(generator.randint(1, 5) for _ in range(rank))
        offsets = tuple(generator.randint(0, dim - 1) for dim in shape)
        sizes = tuple(
            generator.randint(0, shape[axis] - offsets[axis]) for axis in range(rank)
        )
        item_size = generator.choice((1, 2, 4, 8))
        assert byte_ranges(shape, offsets, sizes, item_size) == _reference_byte_ranges(
            shape, offsets, sizes, item_size
        ), (shape, offsets, sizes, item_size)


def test_byte_ranges_rejects_out_of_bounds() -> None:
    with pytest.raises(LayoutError):
        byte_ranges((4, 5), (3, 0), (2, 5), 4)
    with pytest.raises(LayoutError, match="item_size must be positive"):
        byte_ranges((4,), (0,), (1,), 0)
    with pytest.raises(LayoutError, match="must match"):
        byte_ranges((4, 5), (0,), (1, 1), 4)


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
