from __future__ import annotations

from shardferry.planning import plan, validate_plan
from tests.fixtures.generated import generate_sharded_spec


def test_128_rank_source_can_plan_64_and_256_rank_targets() -> None:
    shape = (128, 4)
    for target_world in (64, 256):
        spec, _, _ = generate_sharded_spec(
            shape,
            (128,),
            ("Shard(0)",),
            (target_world,),
            ("Shard(0)",),
        )
        result = plan(spec)
        validate_plan(result, spec)
        assert result["full_gather"] is False
        assert result["total_bytes"] == shape[0] * shape[1] * 4
        assert result["theoretical_min_bytes"] == result["total_bytes"]


def test_128_rank_source_with_2d_mesh_resizes_to_64_rank_target() -> None:
    shape = (128, 64)
    spec, _, _ = generate_sharded_spec(
        shape,
        (16, 8),
        ("Shard(0)", "Shard(1)"),
        (8, 8),
        ("Shard(0)", "Shard(1)"),
    )
    result = plan(spec)
    validate_plan(result, spec)
    assert result["full_gather"] is False
    assert result["total_bytes"] == shape[0] * shape[1] * 4
    assert result["theoretical_min_bytes"] == result["total_bytes"]
    assert result["cost"]["max_fan_in"] <= 4
