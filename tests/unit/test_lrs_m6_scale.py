from __future__ import annotations

import pytest

from shardferry.lrs import (
    CoverageError,
    LRSSimulator,
    Migration,
    RankPhase,
    StaleEpochError,
    Takeover,
    ToyConfig,
)


def _config(world_size: int) -> ToyConfig:
    return ToyConfig(
        world_size=world_size,
        shard_size=1,
        param_count=world_size,
        batch_per_rank=4,
        dataset_size=max(256, world_size * 16),
        seed=7,
    )


@pytest.mark.parametrize(
    ("world_size", "in_flight"),
    [(12, 2), (12, 4), (24, 4), (24, 8)],
)
def test_m6_scale_bitwise(
    world_size: int,
    in_flight: int,
) -> None:
    baseline = LRSSimulator(_config(world_size)).run(16)
    sim = LRSSimulator(_config(world_size))
    sim.run(
        16,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(in_flight)
        ],
        max_in_flight=in_flight,
    )

    assert sim.step_index == baseline.step_index == 16
    assert sim.theta.tobytes() == baseline.theta.tobytes()
    assert sim.momentum.tobytes() == baseline.momentum.tobytes()
    assert sim.loss_history == baseline.loss_history
    assert sim.batch_history == baseline.batch_history
    assert len(sim.proofs) == in_flight
    assert all(proof.steps_lost == 0 for proof in sim.proofs)
    assert all(proof.global_stop == 0 for proof in sim.proofs)
    assert [
        proof.in_flight_at_cutover for proof in sim.proofs
    ] == list(range(in_flight, 0, -1))

    b_ranks = {
        rank
        for rank, cluster in sim.cluster_layout.items()
        if cluster.value == "B"
    }
    a_ranks = {
        rank
        for rank, cluster in sim.cluster_layout.items()
        if cluster.value == "A"
    }
    assert b_ranks == set(range(in_flight))
    assert a_ranks == set(range(in_flight, world_size))


def test_m6_w12_takeover_8plus4_is_bitwise() -> None:
    world_size = 12
    baseline = LRSSimulator(_config(world_size)).run(16)
    sim = LRSSimulator(_config(world_size))
    sim.run(
        12,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(8)
        ],
        max_in_flight=8,
    )
    for shard_id, owner in zip(range(8, 12), range(12, 16)):
        sim.replicate_shadow(shard_id, owner)
    sim.run(16, takeovers=[Takeover(12)])

    assert sim.theta.tobytes() == baseline.theta.tobytes()
    assert sim.momentum.tobytes() == baseline.momentum.tobytes()
    assert sim.loss_history == baseline.loss_history
    proof = sim.takeover_proofs[0]
    assert proof.degraded_world_size == 12
    assert proof.b_ranks == tuple(range(8)) + tuple(range(12, 16))
    assert proof.coverage == tuple(range(12))
    assert proof.migrated_shards == tuple(range(8))
    assert proof.shadow_shards == tuple(range(8, 12))
    assert proof.quorum_required == 7
    assert proof.steps_lost == 0
    assert proof.global_stop == 0
    for rank in range(8, 12):
        assert sim.ranks[rank].phase is RankPhase.DEAD
    for rank in range(12, 16):
        assert sim.ranks[rank].phase is RankPhase.ACTIVE
        assert sim.ranks[rank].cluster.value == "B"
    assert {
        rank
        for rank, state in sim.ranks.items()
        if state.phase is RankPhase.ACTIVE
    } == set(range(8)) | set(range(12, 16))


def test_m6_w12_takeover_without_coverage_fails_closed() -> None:
    world_size = 12
    sim = LRSSimulator(_config(world_size))
    sim.run(
        12,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(8)
        ],
        max_in_flight=8,
    )
    with pytest.raises(CoverageError):
        sim.run(16, takeovers=[Takeover(12)])
    assert sim.takeover_proofs == []
    assert sim.ranks[8].phase is RankPhase.ACTIVE
    assert sim.ranks[9].phase is RankPhase.ACTIVE


def test_m6_w12_stale_epoch_fails_closed() -> None:
    world_size = 12
    sim = LRSSimulator(_config(world_size))
    sim.run(
        11,
        [Migration(rank=0, begin_step=4, cutover_step=10)],
        max_in_flight=1,
    )
    assert sim.ranks[0].phase is RankPhase.ACTIVE
    with pytest.raises(StaleEpochError):
        sim.write_as_target(
            0,
            0,
            epoch=1,
            theta=sim.theta[:1],
            momentum=sim.momentum[:1],
        )
