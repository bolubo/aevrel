from __future__ import annotations

import pytest
from math import prod

from aevrel.planning import plan, validate_plan
from tests.fixtures.generated import generate_sharded_spec


@pytest.mark.parametrize(
    "source_world,target_world,shape",
    [
        (8, 5, (64, 32)),
        (16, 7, (128, 32)),
    ],
)
def test_larger_world_size_plans_stay_linear(
    source_world: int,
    target_world: int,
    shape: tuple[int, int],
) -> None:
    spec, _, _ = generate_sharded_spec(
        shape,
        (source_world,),
        ("Shard(0)",),
        (target_world,),
        ("Shard(0)",),
    )
    result = plan(spec)
    validate_plan(result, spec)
    assert result["total_bytes"] == prod(shape) * 4
    assert result["theoretical_min_bytes"] == result["total_bytes"]
    assert len(result["nodes"]) <= source_world * target_world
