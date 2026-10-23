from __future__ import annotations

from pathlib import Path

from aevrel.cas_executor import apply_rank_to_staging
from aevrel.executor import apply
from aevrel.planning import plan
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


def test_cas_staging_matches_executor_target(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    plan_payload = plan(spec)
    apply(plan_payload, spec)

    staging = tmp_path / "cas-staging"
    files = apply_rank_to_staging(
        spec,
        plan_payload,
        1,
        staging,
    )
    assert files
    for relative, staged in files.items():
        normal = (
            tmp_path
            / "work"
            / "target"
            / relative
        )
        assert staged.read_bytes() == normal.read_bytes()


def test_cas_staging_rejects_invalid_rank(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    plan_payload = plan(spec)
    try:
        apply_rank_to_staging(spec, plan_payload, 99, tmp_path / "staging")
    except Exception as exc:
        assert "out of range" in str(exc)
    else:
        raise AssertionError("invalid rank was accepted")
