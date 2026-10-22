from __future__ import annotations

import pytest

from shardferry.lrs import (
    DigestMismatchError,
    LRSSimulator,
    Migration,
    ShardBlob,
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


def test_rank_local_rng_migration_is_bitwise() -> None:
    baseline = LRSSimulator(_config()).run(16)
    sim = LRSSimulator(_config()).run(
        16,
        [Migration(rank=1, begin_step=4, cutover_step=10)],
    )

    assert sim.theta.tobytes() == baseline.theta.tobytes()
    assert sim.momentum.tobytes() == baseline.momentum.tobytes()
    assert sim.loss_history == baseline.loss_history
    assert sim.batch_history == baseline.batch_history
    assert sim.samplers[1].capture() == baseline.samplers[1].capture()
    assert sim.samplers[1].draw() == baseline.samplers[1].draw()

    proof = sim.proofs[0]
    assert proof.live_cutover_digest == proof.target_cutover_digest
    assert proof.snapshot_digest != proof.live_cutover_digest


def test_rank_rng_is_per_rank() -> None:
    sim = LRSSimulator(_config())
    sim.run(4)
    assert sim.samplers[0].capture() != sim.samplers[1].capture()

    sim.run(
        16,
        [Migration(rank=1, begin_step=4, cutover_step=10)],
    )
    control = LRSSimulator(_config()).run(16)
    assert sim.samplers[0].capture() == control.samplers[0].capture()
    assert sim.samplers[2].capture() == control.samplers[2].capture()


def test_rank_rng_is_part_of_blob_digest() -> None:
    left = ShardBlob.capture(
        step=0,
        shard_id=1,
        epoch=0,
        theta=(1.0,),
        momentum=(0.0,),
        aux=b"rng-a",
    )
    right = ShardBlob.capture(
        step=0,
        shard_id=1,
        epoch=0,
        theta=(1.0,),
        momentum=(0.0,),
        aux=b"rng-b",
    )
    assert left.digest != right.digest
    with pytest.raises(DigestMismatchError):
        left.assert_matches(right)


def test_cutover_rejects_wrong_target_rng() -> None:
    sim = LRSSimulator(_config())
    sim.run(4)
    spec = Migration(rank=1, begin_step=4, cutover_step=10)
    sim._begin_migration(spec)
    for step in range(4, 11):
        sim._run_step(step)

    runtime = sim._runtimes[1]
    original = runtime.transfer.acked_blob(10)
    corrupt = ShardBlob.capture(
        step=10,
        shard_id=1,
        epoch=original.epoch,
        theta=original.theta,
        momentum=original.momentum,
        aux=b"wrong-rng-state",
    )
    runtime.transfer._blobs[10] = corrupt

    with pytest.raises(DigestMismatchError):
        sim._cutover(spec)
    assert sim.proofs == []
    assert sim.ranks[1].phase.value == "in_flight"
