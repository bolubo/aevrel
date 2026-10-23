from __future__ import annotations

from aevrel.coordinator import expected_rank_work
from aevrel.planning import plan
from tests.fixtures.generated import generate_sharded_spec


def test_expected_rank_work_matches_plan_target_ranks() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    plan_payload = plan(spec)

    expected = expected_rank_work(plan_payload, spec)

    assert set(expected) == {0, 1, 2}
    assert [expected[rank]["chunks"] for rank in range(3)] == [2, 4, 2]
    assert [
        expected[rank]["bytes_planned"] for rank in range(3)
    ] == [48, 48, 32]
    assert all(
        expected[rank]["tensors"]
        == {"w": expected[rank]["chunks"]}
        for rank in range(3)
    )
    assert sum(
        row["bytes_planned"] for row in expected.values()
    ) == plan_payload["total_bytes"]
