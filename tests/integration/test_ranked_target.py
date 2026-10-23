from __future__ import annotations

from pathlib import Path

import numpy as np

from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.targets import target_file_path
from shardferry.verification import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import assert_bitwise_equal, reference_reshard


def test_target_rank_can_apply_and_verify_independently(tmp_path) -> None:
    spec, arrays, tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    expected = reference_reshard(tensor, (3, 1), ("Shard(0)", "Replicate()"))

    for rank in range(3):
        target_root = tmp_path / f"target-rank{rank}"
        report = apply(
            plan_payload,
            spec,
            target_rank=rank,
            target_root=target_root,
        )
        assert report["chunks"] == sum(
            1 for node in plan_payload["nodes"] if node["target_rank"] == rank
        )
        verify(
            spec,
            plan_payload,
            target_rank=rank,
            target_root=target_root,
        )
        actual = np.frombuffer(
            target_file_path(spec, "w", rank, root=target_root).read_bytes(),
            dtype=np.float32,
        ).reshape(expected[rank][1].shape)
        assert_bitwise_equal(actual, expected[rank][1])
