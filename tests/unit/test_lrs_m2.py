from __future__ import annotations

import numpy as np
import pytest

from shardferry.lrs import (
    Cluster,
    CoverageError,
    FencedError,
    LRSSimulator,
    LrsError,
    Migration,
    RankPhase,
    ToyConfig,
)


def _config(
    *,
    dtype: str = "float64",
    reduction: str = "canonical",
) -> ToyConfig:
    return ToyConfig(
        world_size=8,
        shard_size=1,
        param_count=8,
        batch_per_rank=4,
        dataset_size=128,
        seed=7,
        dtype=dtype,
        reduction=reduction,
    )


@pytest.mark.parametrize("in_flight", [1, 2, 3])
def test_m2_w8_concurrent_bitwise(in_flight: int) -> None:
    config = _config()
    baseline = LRSSimulator(config).run(24)
    migrations = [
        Migration(rank=rank, begin_step=4, cutover_step=12)
        for rank in range(in_flight)
    ]
    migrated = LRSSimulator(config).run(
        24,
        migrations,
        max_in_flight=in_flight,
    )

    assert migrated.step_index == baseline.step_index == 24
    assert migrated.theta.tobytes() == baseline.theta.tobytes()
    assert migrated.momentum.tobytes() == baseline.momentum.tobytes()
    assert migrated.loss_history == baseline.loss_history
    assert migrated.batch_history == baseline.batch_history
    assert migrated.cluster_layout == {
        rank: (Cluster.B if rank < in_flight else Cluster.A)
        for rank in range(8)
    }
    assert [proof.rank for proof in migrated.proofs] == list(
        range(in_flight)
    )
    assert [proof.temp_owner for proof in migrated.proofs] == list(
        range(in_flight, 2 * in_flight)
    )
    assert [
        proof.in_flight_at_cutover for proof in migrated.proofs
    ] == list(range(in_flight, 0, -1))
    for proof in migrated.proofs:
        assert proof.steps_lost == 0
        assert proof.global_stop == 0
        assert proof.snapshot_step == proof.begin_step - 1
        assert proof.live_cutover_digest == proof.target_cutover_digest
        assert proof.coverage == tuple(range(8))
    assert migrated.active_migrations == ()

    for step in range(4, 13):
        assignments = migrated.assignment_history[step]
        assert set(assignments) == set(range(in_flight, 8))
        assigned = [
            sample_id
            for samples in assignments.values()
            for sample_id in samples
        ]
        assert sorted(assigned) == sorted(migrated.batch_history[step])
        assert len(assigned) == 32
        assert all(len(samples) >= 4 for samples in assignments.values())


def test_m2_staggered_migrations_bitwise() -> None:
    config = _config()
    baseline = LRSSimulator(config).run(30)
    migrations = [
        Migration(rank=0, begin_step=4, cutover_step=10),
        Migration(rank=1, begin_step=6, cutover_step=12),
        Migration(rank=2, begin_step=8, cutover_step=14),
    ]
    migrated = LRSSimulator(config).run(
        30,
        migrations,
        max_in_flight=3,
    )

    assert migrated.theta.tobytes() == baseline.theta.tobytes()
    assert migrated.momentum.tobytes() == baseline.momentum.tobytes()
    assert migrated.loss_history == baseline.loss_history
    assert migrated.batch_history == baseline.batch_history
    assert migrated.cluster_layout == {
        0: Cluster.B,
        1: Cluster.B,
        2: Cluster.B,
        3: Cluster.A,
        4: Cluster.A,
        5: Cluster.A,
        6: Cluster.A,
        7: Cluster.A,
    }
    assert [proof.rank for proof in migrated.proofs] == [0, 1, 2]
    assert [proof.temp_owner for proof in migrated.proofs] == [3, 4, 5]
    assert migrated.active_migrations == ()

    assert len(migrated.assignment_history[4]) == 7
    assert len(migrated.assignment_history[6]) == 6
    assert len(migrated.assignment_history[8]) == 5
    assert len(migrated.assignment_history[11]) == 6
    assert len(migrated.assignment_history[13]) == 7
    assert len(migrated.assignment_history[15]) == 8


def test_m2_sequential_waves_bitwise() -> None:
    config = _config()
    baseline = LRSSimulator(config).run(30)
    migrations = [
        Migration(rank=0, begin_step=4, cutover_step=10),
        Migration(rank=1, begin_step=4, cutover_step=10),
        Migration(rank=2, begin_step=12, cutover_step=18),
        Migration(rank=3, begin_step=12, cutover_step=18),
    ]
    migrated = LRSSimulator(config).run(
        30,
        migrations,
        max_in_flight=2,
    )

    assert migrated.theta.tobytes() == baseline.theta.tobytes()
    assert migrated.momentum.tobytes() == baseline.momentum.tobytes()
    assert migrated.loss_history == baseline.loss_history
    assert migrated.batch_history == baseline.batch_history
    assert migrated.cluster_layout == {
        0: Cluster.B,
        1: Cluster.B,
        2: Cluster.B,
        3: Cluster.B,
        4: Cluster.A,
        5: Cluster.A,
        6: Cluster.A,
        7: Cluster.A,
    }
    assert [proof.rank for proof in migrated.proofs] == [0, 1, 2, 3]
    assert [proof.temp_owner for proof in migrated.proofs] == [2, 3, 4, 5]


def test_m2_fp32_f3_within_tolerance() -> None:
    config = _config(dtype="float32", reduction="rank")
    baseline = LRSSimulator(config).run(24)
    migrations = [
        Migration(rank=rank, begin_step=4, cutover_step=12)
        for rank in range(3)
    ]
    migrated = LRSSimulator(config).run(
        24,
        migrations,
        max_in_flight=3,
    )

    assert np.allclose(migrated.theta, baseline.theta, atol=1e-6, rtol=0)
    assert np.allclose(
        migrated.momentum,
        baseline.momentum,
        atol=1e-5,
        rtol=0,
    )
    assert np.allclose(
        migrated.loss_history,
        baseline.loss_history,
        atol=1e-5,
        rtol=0,
    )
    assert migrated.batch_history == baseline.batch_history
    for proof in migrated.proofs:
        assert proof.live_cutover_digest == proof.target_cutover_digest


def test_m2_f_exceeded_fails_closed() -> None:
    migrations = [
        Migration(rank=rank, begin_step=4, cutover_step=12)
        for rank in range(3)
    ]
    with pytest.raises(LrsError):
        LRSSimulator(_config()).run(
            24,
            migrations,
            max_in_flight=2,
        )


def test_m2_temp_owner_conflict_fails_closed() -> None:
    sim = LRSSimulator(_config())
    sim.run(4, max_in_flight=2)
    sim._begin_migration(
        Migration(rank=0, begin_step=4, cutover_step=14),
        reserved=set(),
    )
    with pytest.raises(LrsError):
        sim._begin_migration(
            Migration(rank=1, begin_step=4, cutover_step=12),
            reserved=set(),
        )


def test_m2_source_fence_with_multiple_inflight() -> None:
    sim = LRSSimulator(_config())
    sim.run(4, max_in_flight=2)
    reserved = {0, 1}
    sim._begin_migration(
        Migration(rank=0, begin_step=4, cutover_step=12),
        reserved=reserved,
    )
    sim._begin_migration(
        Migration(rank=1, begin_step=4, cutover_step=12),
        reserved=reserved,
    )
    sim._run_step(4)

    assert len(sim.active_migrations) == 2
    for rank in (0, 1):
        window = slice(rank, rank + 1)
        with pytest.raises(FencedError):
            sim.write_as_source(
                rank,
                rank,
                epoch=0,
                theta=sim.theta[window],
                momentum=sim.momentum[window],
            )
        with pytest.raises(FencedError):
            sim.write_as_target(
                rank,
                rank,
                epoch=1,
                theta=sim.theta[window],
                momentum=sim.momentum[window],
            )


def test_m2_all_targets_join_and_ownership_is_final() -> None:
    sim = LRSSimulator(_config())
    sim.run(
        24,
        [
            Migration(rank=rank, begin_step=4, cutover_step=12)
            for rank in range(3)
        ],
        max_in_flight=3,
    )

    assert all(
        state.phase is RankPhase.ACTIVE
        for state in sim.ranks.values()
    )
    assert sim.active_migrations == ()
    for rank in range(3):
        assert sim.ranks[rank].cluster is Cluster.B
        assert sim.ranks[rank].source_fenced
        assert sim.ownership.get(rank).primary == rank
        assert sim.ownership.get(rank).epoch == 2
    for rank in range(3, 8):
        assert sim.ranks[rank].cluster is Cluster.A
        assert not sim.ranks[rank].source_fenced
        assert sim.ownership.get(rank).primary == rank
        assert sim.ownership.get(rank).epoch == 0


def test_m2_coverage_failure_fails_closed() -> None:
    sim = LRSSimulator(_config())
    sim.ownership.get(7).primary = None  # type: ignore[assignment]
    with pytest.raises(CoverageError):
        sim.run(1, max_in_flight=2)


def test_m2_duplicate_rank_fails_closed() -> None:
    with pytest.raises(LrsError):
        LRSSimulator(_config()).run(
            24,
            [
                Migration(rank=0, begin_step=4, cutover_step=12),
                Migration(rank=0, begin_step=6, cutover_step=14),
            ],
            max_in_flight=2,
        )


def test_m2_all_ranks_can_use_migrated_b_temp_owners() -> None:
    config = ToyConfig(
        world_size=4,
        shard_size=1,
        param_count=4,
        batch_per_rank=2,
        dataset_size=32,
        seed=7,
    )
    baseline = LRSSimulator(config).run(7)
    migrated = LRSSimulator(config).run(
        7,
        [
            Migration(rank=0, begin_step=1, cutover_step=3),
            Migration(rank=1, begin_step=1, cutover_step=3),
            Migration(rank=2, begin_step=4, cutover_step=6),
            Migration(rank=3, begin_step=4, cutover_step=6),
        ],
        max_in_flight=2,
    )
    assert migrated.theta.tobytes() == baseline.theta.tobytes()
    assert migrated.momentum.tobytes() == baseline.momentum.tobytes()
    assert migrated.loss_history == baseline.loss_history
    assert migrated.batch_history == baseline.batch_history
    assert migrated.cluster_layout == {
        rank: Cluster.B for rank in range(4)
    }
    assert [proof.rank for proof in migrated.proofs] == [0, 1, 2, 3]
    assert [proof.temp_owner for proof in migrated.proofs] == [2, 3, 0, 1]
    assert all(proof.steps_lost == 0 for proof in migrated.proofs)
    assert all(proof.global_stop == 0 for proof in migrated.proofs)
