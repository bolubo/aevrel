from __future__ import annotations

import numpy as np
import pytest

from shardferry.lrs import CoverageError, LrsError, RankPhase
from shardferry.lrs.tp_pp import (
    TPPPBaseline,
    TPPPConfig,
    TPPPSimulator,
)


def _setup():
    config = TPPPConfig()
    sim = TPPPSimulator(config)
    baseline = TPPPBaseline(
        config,
        sim.x,
        sim.y,
        sim.w1_init,
        sim.w2_init,
    )
    return config, sim, baseline


def test_m5_tp_rank_migration_matches_baseline() -> None:
    config, sim, baseline = _setup()
    baseline.run(8)
    sim.run(2)
    spec = sim.begin_tp_migration(1, cutover_step=5)
    sim.run(8)

    assert spec.rank == 1
    w1, w2 = sim.reconstruct()
    assert np.allclose(w1, baseline.w1, atol=1e-9)
    assert np.allclose(w2, baseline.w2, atol=1e-9)
    assert np.allclose(
        sim.loss_history,
        baseline.loss_history,
        atol=1e-12,
    )

    proof = sim.tp_proofs[0]
    assert proof.rank == 1
    assert proof.temp_owner == 0
    assert proof.tp_epoch == 1
    assert proof.pp_epoch == 0
    assert proof.live_digest == proof.target_digest
    assert proof.collective_owners_after == (0, 1)
    assert proof.steps_lost == 0
    assert proof.global_stop == 0
    assert proof.to_dict()["tp_epoch"] == 1

    assert sim.ranks[1].phase is RankPhase.ACTIVE
    assert sim.ranks[1].cluster.value == "B"
    assert sim.ranks[1].source_fenced
    post = [
        record
        for record in sim.collective_log
        if record.pp == 0 and record.tp_epoch == 1
    ]
    assert post
    assert all(record.owners == (0, 1) for record in post)


def test_m5_pp_stage_migration_matches_baseline() -> None:
    config, sim, baseline = _setup()
    baseline.run(8)
    sim.run(2)
    sim.shadow_pp_stage(1)
    sim.cutover_pp_stage(1)
    sim.run(8)

    w1, w2 = sim.reconstruct()
    assert np.allclose(w1, baseline.w1, atol=1e-9)
    assert np.allclose(w2, baseline.w2, atol=1e-9)
    assert np.allclose(
        sim.loss_history,
        baseline.loss_history,
        atol=1e-12,
    )

    proof = sim.pp_proofs[0]
    assert proof.pp == 1
    assert proof.step == 2
    assert proof.logical_ranks == (2, 3)
    assert proof.old_owners == (2, 3)
    assert proof.new_owners == (4, 5)
    assert proof.tp_epoch == 1
    assert proof.pp_epoch == 1
    assert proof.steps_lost == 0
    assert proof.global_stop == 0
    assert len(proof.digests) == 2
    assert proof.to_dict()["new_owners"] == [4, 5]

    assert sim.ranks[2].phase is RankPhase.DEAD
    assert sim.ranks[3].phase is RankPhase.DEAD
    assert sim.ranks[4].phase is RankPhase.ACTIVE
    assert sim.ranks[5].phase is RankPhase.ACTIVE
    assert sim.ranks[4].cluster.value == "B"
    assert sim.ranks[5].cluster.value == "B"

    post_collectives = [
        record
        for record in sim.collective_log
        if record.pp == 1 and record.pp_epoch == 1
    ]
    assert post_collectives
    assert all(record.owners == (4, 5) for record in post_collectives)
    assert all(record.tp_epoch == 1 for record in post_collectives)
    post_pipeline = [
        record
        for record in sim.pipeline_log
        if record.to_pp == 1 and record.pp_epoch == 1
    ]
    assert post_pipeline
    assert all(record.to_owners == (4, 5) for record in post_pipeline)


def test_m5_tp_collective_requires_all_parts() -> None:
    _config, sim, _baseline = _setup()
    sim.ranks[1].phase = RankPhase.FENCED
    with pytest.raises(LrsError):
        sim.run(1)


def test_m5_stale_pp_shadow_fails_closed() -> None:
    _config, sim, _baseline = _setup()
    sim.run(2)
    sim.shadow_pp_stage(1)
    sim.run(3)
    with pytest.raises(CoverageError):
        sim.cutover_pp_stage(1)
    assert sim.pp_proofs == []
    assert sim.ranks[2].phase is RankPhase.ACTIVE
    assert sim.ranks[3].phase is RankPhase.ACTIVE


def test_m5_tp_migration_requires_active_peer() -> None:
    _config, sim, _baseline = _setup()
    sim.run(2)
    sim.ranks[0].phase = RankPhase.FENCED
    with pytest.raises(LrsError):
        sim.begin_tp_migration(1, cutover_step=5)
