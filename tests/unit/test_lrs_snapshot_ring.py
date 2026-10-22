from __future__ import annotations

from shardferry.lrs.snapshot_ring import NeighborBackup, SnapshotRing
from shardferry.lrs.state_closure import RNGState, TrainingState


def _state(step: int) -> TrainingState:
    return TrainingState(
        step=step,
        model={},
        optimizer={},
        rng=RNGState(python=b"", numpy=b"", torch=b""),
        generator=None,
        scaler=None,
        extra={},
    )


def test_snapshot_ring_evicts_oldest() -> None:
    ring = SnapshotRing(capacity=2)
    ring.push(0, _state(0))
    ring.push(1, _state(1))
    ring.push(2, _state(2))
    assert ring.steps() == (1, 2)
    assert ring.get(0) is None
    assert ring.get(1).step == 1
    assert ring.latest().step == 2


def test_neighbor_backup_restores() -> None:
    backup = NeighborBackup()
    state = _state(7)
    backup.backup(
        source_rank=1,
        target_rank=2,
        state=state,
    )
    source, restored = backup.restore(2)
    assert source == 1
    assert restored.step == 7
    backup.discard(2)
    assert backup.restore(2) is None
