"""Per-step host snapshot ring and neighbor backup (ElasWave-inspired)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .state_closure import TrainingState


@dataclass(frozen=True)
class SnapshotEntry:
    step: int
    state: TrainingState


class SnapshotRing:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._entries: deque[SnapshotEntry] = deque()

    def push(self, step: int, state: TrainingState) -> None:
        if self._entries and step <= self._entries[-1].step:
            raise ValueError("snapshot steps must increase")
        self._entries.append(SnapshotEntry(step=step, state=state))
        while len(self._entries) > self.capacity:
            self._entries.popleft()

    def latest(self) -> SnapshotEntry | None:
        if not self._entries:
            return None
        return self._entries[-1]

    def get(self, step: int) -> TrainingState | None:
        for entry in self._entries:
            if entry.step == step:
                return entry.state
        return None

    def steps(self) -> tuple[int, ...]:
        return tuple(entry.step for entry in self._entries)


class NeighborBackup:
    """Rank i backs up rank (i+1) mod n; restore by target rank."""

    def __init__(self) -> None:
        self._backups: dict[int, tuple[int, TrainingState]] = {}

    def backup(
        self,
        *,
        source_rank: int,
        target_rank: int,
        state: TrainingState,
    ) -> None:
        self._backups[target_rank] = (source_rank, state)

    def restore(
        self,
        target_rank: int,
    ) -> tuple[int, TrainingState] | None:
        return self._backups.get(target_rank)

    def discard(self, target_rank: int) -> None:
        self._backups.pop(target_rank, None)
