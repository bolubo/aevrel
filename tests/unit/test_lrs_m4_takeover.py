from __future__ import annotations

import pytest

from aevrel.lrs import (
    CoverageError,
    LRSSimulator,
    LrsError,
    Migration,
    QuorumError,
    RankPhase,
    Takeover,
    TakeoverError,
    ToyConfig,
)


def _config() -> ToyConfig:
    return ToyConfig(
        world_size=8,
        shard_size=1,
        param_count=8,
        batch_per_rank=4,
        dataset_size=128,
        seed=7,
    )


def _migrations() -> list[Migration]:
    return [
        Migration(rank=rank, begin_step=4, cutover_step=10)
        for rank in range(6)
    ]


def _prepare(owner_6: int, owner_7: int) -> LRSSimulator:
    sim = LRSSimulator(_config())
    sim.run(12, _migrations(), max_in_flight=6)
    sim.replicate_shadow(6, owner_6)
    sim.replicate_shadow(7, owner_7)
    return sim


def test_m4_takeover_with_standbys_is_bitwise() -> None:
    baseline = LRSSimulator(_config()).run(24)
    sim = _prepare(8, 9)
    sim.run(24, takeovers=[Takeover(12)])

    assert sim.theta.tobytes() == baseline.theta.tobytes()
    assert sim.momentum.tobytes() == baseline.momentum.tobytes()
    assert sim.loss_history == baseline.loss_history
    assert sim.batch_history == baseline.batch_history
    assert sim.step_index == 24
    assert sim.active_migrations == ()
    assert len(sim.takeover_proofs) == 1

    proof = sim.takeover_proofs[0]
    assert proof.step == 12
    assert proof.degraded_world_size == 8
    assert proof.b_ranks == (0, 1, 2, 3, 4, 5, 8, 9)
    assert proof.coverage == tuple(range(8))
    assert proof.migrated_shards == tuple(range(6))
    assert proof.shadow_shards == (6, 7)
    assert proof.quorum_required == 5
    assert proof.quorum_votes == proof.b_ranks
    assert proof.steps_lost == 0
    assert proof.global_stop == 0
    assert len(proof.digests) == 8

    assert sim.ranks[6].phase is RankPhase.DEAD
    assert sim.ranks[7].phase is RankPhase.DEAD
    assert sim.ranks[8].phase is RankPhase.ACTIVE
    assert sim.ranks[9].phase is RankPhase.ACTIVE
    assert {
        rank
        for rank, state in sim.ranks.items()
        if state.phase is RankPhase.ACTIVE
    } == {0, 1, 2, 3, 4, 5, 8, 9}
    assert sim.ownership.get(6).primary == 8
    assert sim.ownership.get(7).primary == 9


def test_m4_degraded_world_smaller_than_original() -> None:
    baseline = LRSSimulator(_config()).run(24)
    sim = _prepare(0, 1)
    sim.run(24, takeovers=[Takeover(12)])

    assert sim.theta.tobytes() == baseline.theta.tobytes()
    assert sim.momentum.tobytes() == baseline.momentum.tobytes()
    assert sim.loss_history == baseline.loss_history
    proof = sim.takeover_proofs[0]
    assert proof.degraded_world_size == 6
    assert proof.b_ranks == (0, 1, 2, 3, 4, 5)
    assert proof.coverage == tuple(range(8))
    assert proof.shadow_shards == (6, 7)
    assert proof.quorum_required == 5
    assert sim.ownership.get(6).primary == 0
    assert sim.ownership.get(7).primary == 1
    assert {
        rank
        for rank, state in sim.ranks.items()
        if state.phase is RankPhase.ACTIVE
    } == {0, 1, 2, 3, 4, 5}


def test_m4_takeover_fails_without_coverage() -> None:
    sim = LRSSimulator(_config())
    sim.run(12, _migrations(), max_in_flight=6)

    with pytest.raises(CoverageError):
        sim.run(24, takeovers=[Takeover(12)])

    assert sim.takeover_proofs == []
    assert sim.step_index == 12
    assert sim.ranks[6].phase is RankPhase.ACTIVE
    assert sim.ranks[7].phase is RankPhase.ACTIVE


def test_m4_takeover_fails_quorum() -> None:
    sim = _prepare(8, 9)
    with pytest.raises(QuorumError):
        sim.run(24, takeovers=[Takeover(12, quorum=9)])

    assert sim.takeover_proofs == []
    assert sim.ranks[6].phase is RankPhase.ACTIVE


def test_m4_stale_shadow_fails_closed() -> None:
    sim = LRSSimulator(_config())
    sim.run(11, _migrations(), max_in_flight=6)
    sim.replicate_shadow(6, 8)
    sim.replicate_shadow(7, 9)
    sim.run(12)

    with pytest.raises(CoverageError):
        sim.run(24, takeovers=[Takeover(12)])

    assert sim.takeover_proofs == []


def test_m4_inflight_migration_blocks_takeover() -> None:
    sim = LRSSimulator(_config())
    sim.run(4)
    sim._begin_migration(Migration(rank=0, begin_step=4, cutover_step=20))

    with pytest.raises(TakeoverError):
        sim.run(6, takeovers=[Takeover(5)])

    assert sim.takeover_proofs == []


def test_m4_migration_must_finish_before_takeover() -> None:
    with pytest.raises(LrsError):
        LRSSimulator(_config()).run(
            24,
            [Migration(rank=0, begin_step=4, cutover_step=12)],
            max_in_flight=1,
            takeovers=[Takeover(10)],
        )


def test_m4_takeover_proof_serializes() -> None:
    sim = _prepare(8, 9)
    sim.run(24, takeovers=[Takeover(12)])
    payload = sim.takeover_proofs[0].to_dict()

    assert payload["step"] == 12
    assert payload["degraded_world_size"] == 8
    assert payload["b_ranks"] == [0, 1, 2, 3, 4, 5, 8, 9]
    assert payload["coverage"] == list(range(8))
    assert payload["shadow_shards"] == [6, 7]
    assert len(payload["digests"]) == 8
