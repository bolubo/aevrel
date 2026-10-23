from __future__ import annotations

from aevrel.planning import plan, validate_plan
from tests.fixtures.generated import generate_sharded_spec
from tests.oracle.reference import (
    assert_bitwise_equal,
    materialize_plan,
    reference_reshard,
)


def _check(
    tensor_shape: tuple[int, ...],
    source_shape: tuple[int, ...],
    source_placements: tuple[str, ...],
    target_shape: tuple[int, ...],
    target_placements: tuple[str, ...],
) -> None:
    spec, arrays, tensor = generate_sharded_spec(
        tensor_shape,
        source_shape,
        source_placements,
        target_shape,
        target_placements,
    )
    result = plan(spec)
    validate_plan(result, spec)
    actual = materialize_plan(result, spec, arrays)
    expected = reference_reshard(tensor, target_shape, target_placements)
    for rank, (_, expected_array) in expected.items():
        assert_bitwise_equal(actual[("w", rank)], expected_array)
    assert result["total_bytes"] == sum(
        array.nbytes for _, array in expected.values()
    )


def test_4to1_and_1to4() -> None:
    _check((8, 4), (4,), ("Shard(0)",), (1,), ("Shard(0)",))
    _check((8, 4), (1,), ("Shard(0)",), (4,), ("Shard(0)",))


def test_1d_to_2d_and_back() -> None:
    _check(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
    )
    _check(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (4,),
        ("Shard(0)",),
    )
