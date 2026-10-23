from __future__ import annotations

import numpy as np
import pytest

from aevrel.lrs import (
    Cluster,
    CoverageError,
    DigestMismatchError,
    FencedError,
    LRSSimulator,
    LrsError,
    Migration,
    RankPhase,
    ShardBlob,
    StaleEpochError,
    ToyConfig,
)


def _two_migrations() -> list[Migration]:
    return [
        Migration(rank=0, begin_step=4, cutover_step=10),
        Migration(rank=1, begin_step=12, cutover_step=18),
    ]


def test_m1_two_plus_two_is_bitwise_identical() -> None:
    baseline = LRSSimulator(ToyConfig())
    baseline.run(24)

    migrated = LRSSimulator(ToyConfig())
    migrated.run(24, _two_migrations())

    assert migrated.step_index == baseline.step_index == 24
    assert migrated.theta.tobytes() == baseline.theta.tobytes()
    assert migrated.momentum.tobytes() == baseline.momentum.tobytes()
    assert migrated.loss_history == baseline.loss_history
    assert migrated.batch_history == baseline.batch_history
    assert migrated.cluster_layout == {
        0: Cluster.B,
        1: Cluster.B,
        2: Cluster.A,
        3: Cluster.A,
    }
    assert len(migrated.proofs) == 2


def test_m1_float32_rank_reduction_within_tolerance() -> None:
    config = ToyConfig(dtype="float32", reduction="rank")
    baseline = LRSSimulator(config).run(24)
    migrated = LRSSimulator(config).run(24, _two_migrations())

    assert migrated.step_index == baseline.step_index == 24
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
    assert migrated.cluster_layout == {
        0: Cluster.B,
        1: Cluster.B,
        2: Cluster.A,
        3: Cluster.A,
    }
    for proof in migrated.proofs:
        assert proof.live_cutover_digest == proof.target_cutover_digest


def test_m1_proofs_are_complete() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(24, _two_migrations())

    assert [proof.rank for proof in sim.proofs] == [0, 1]
    assert [proof.temp_owner for proof in sim.proofs] == [1, 2]
    for proof in sim.proofs:
        assert proof.snapshot_step == proof.begin_step - 1
        assert proof.steps_lost == 0
        assert proof.global_stop == 0
        assert proof.source_cluster is Cluster.A
        assert proof.target_cluster is Cluster.B
        assert proof.snapshot_digest.startswith("sha256:")
        assert proof.live_cutover_digest == proof.target_cutover_digest
        assert proof.coverage == (0, 1, 2, 3)
        assert proof.quorum_votes == (0, 1, 2, 3)
        assert proof.target_epoch == proof.cutover_epoch + 1
        assert ("cutover", proof.cutover_step) in proof.timeline
        assert ("fence", proof.cutover_step) in proof.timeline


def test_m1_proof_serializes() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(11, [Migration(rank=0, begin_step=4, cutover_step=10)])
    payload = sim.proofs[0].to_dict()

    assert payload["rank"] == 0
    assert payload["snapshot_step"] == 3
    assert payload["temp_owner"] == 1
    assert payload["coverage"] == [0, 1, 2, 3]
    assert payload["steps_lost"] == 0
    assert payload["global_stop"] == 0
    assert payload["timeline"]


def test_m1_source_fence_and_stale_epoch_are_rejected() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(11, [Migration(rank=0, begin_step=4, cutover_step=10)])

    assert sim.ranks[0].cluster is Cluster.B
    assert sim.ranks[0].source_fenced
    with pytest.raises(FencedError):
        sim.write_as_source(
            0,
            0,
            epoch=0,
            theta=sim.theta[:2],
            momentum=sim.momentum[:2],
        )
    with pytest.raises(StaleEpochError):
        sim.write_as_target(
            0,
            0,
            epoch=1,
            theta=sim.theta[:2],
            momentum=sim.momentum[:2],
        )
    sim.write_as_target(
        0,
        0,
        epoch=2,
        theta=sim.theta[:2],
        momentum=sim.momentum[:2],
    )


def test_m1_source_is_fenced_while_in_flight() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(4)
    spec = Migration(rank=0, begin_step=4, cutover_step=10)
    sim._begin_migration(spec)
    sim._run_step(4)

    assert sim.ranks[0].phase is RankPhase.IN_FLIGHT
    with pytest.raises(FencedError):
        sim.write_as_source(
            0,
            0,
            epoch=0,
            theta=sim.theta[:2],
            momentum=sim.momentum[:2],
        )


def test_m1_target_cannot_write_before_cutover() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(4)
    sim._begin_migration(Migration(rank=0, begin_step=4, cutover_step=10))
    sim._run_step(4)

    with pytest.raises(FencedError):
        sim.write_as_target(
            0,
            0,
            epoch=1,
            theta=sim.theta[:2],
            momentum=sim.momentum[:2],
        )


def test_m1_duplicate_cutover_fails_closed() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(11, [Migration(rank=0, begin_step=4, cutover_step=10)])
    with pytest.raises(LrsError):
        sim._cutover(Migration(rank=0, begin_step=4, cutover_step=10))


def test_m1_work_redistribution_covers_global_batch() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(11, [Migration(rank=0, begin_step=4, cutover_step=10)])

    for step in range(4, 11):
        assignments = sim.assignment_history[step]
        assert set(assignments) == {1, 2, 3}
        assigned = [
            sample_id
            for samples in assignments.values()
            for sample_id in samples
        ]
        assert sorted(assigned) == sorted(sim.batch_history[step])
        assert len(assigned) == len(set(assigned))
        assert len(assigned) == sim.config.world_size * sim.config.batch_per_rank
        assert all(
            len(samples) >= sim.config.batch_per_rank
            for samples in assignments.values()
        )


def test_m1_coverage_failure_fails_closed() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.ownership.get(3).primary = None  # type: ignore[assignment]
    with pytest.raises(CoverageError):
        sim.run(1)


def test_m1_digest_mismatch_is_detected() -> None:
    left = ShardBlob.capture(
        step=0,
        shard_id=0,
        epoch=0,
        theta=(1.0, 2.0),
        momentum=(0.0, 0.0),
    )
    right = ShardBlob.capture(
        step=0,
        shard_id=0,
        epoch=0,
        theta=(1.0, 3.0),
        momentum=(0.0, 0.0),
    )
    with pytest.raises(DigestMismatchError):
        left.assert_matches(right)


def test_m1_delta_log_is_contiguous_and_replayable() -> None:
    sim = LRSSimulator(ToyConfig())
    sim.run(11, [Migration(rank=0, begin_step=4, cutover_step=10)])

    log = sim.delta_logs[0]
    assert log.records[0].step == 0
    assert log.records[-1].step == 10
    for previous, current in zip(log.records, log.records[1:]):
        assert current.step == previous.step + 1
    replayed = log.replay(log.records[3], 10)
    assert replayed.digest == log.records[10].digest


def test_m1_overlapping_migrations_fail_closed() -> None:
    with pytest.raises(LrsError):
        LRSSimulator(ToyConfig()).run(
            24,
            [
                Migration(rank=0, begin_step=4, cutover_step=10),
                Migration(rank=1, begin_step=10, cutover_step=16),
            ],
        )


def test_m1_cutover_outside_run_fails_closed() -> None:
    with pytest.raises(LrsError):
        LRSSimulator(ToyConfig()).run(
            10,
            [Migration(rank=0, begin_step=4, cutover_step=10)],
        )
