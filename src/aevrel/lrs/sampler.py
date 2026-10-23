"""Per-rank sampler and RNG state for LRS rank-local closure."""

from __future__ import annotations

import numpy as np

from ..state import NumpyRandomProvider


class RankSampler:
    """A disjoint data-shard sampler whose RNG is part of rank state."""

    def __init__(
        self,
        *,
        rank: int,
        seed: int,
        start: int,
        end: int,
        batch_size: int,
    ) -> None:
        if rank < 0:
            raise ValueError("rank must be non-negative")
        if start < 0 or end <= start:
            raise ValueError("invalid sampler range")
        if batch_size < 1 or batch_size > end - start:
            raise ValueError("batch_size does not fit the rank shard")
        self.rank = rank
        self.start = start
        self.end = end
        self.batch_size = batch_size
        self.rng = np.random.RandomState(seed)
        self.provider = NumpyRandomProvider(self.rng)

    def draw(self) -> tuple[int, ...]:
        values = self.rng.choice(
            np.arange(self.start, self.end),
            size=self.batch_size,
            replace=False,
        )
        return tuple(int(value) for value in values)

    def capture(self) -> bytes:
        return self.provider.capture()

    def restore(self, data: bytes) -> None:
        self.provider.restore(data)

    @classmethod
    def from_state(
        cls,
        *,
        rank: int,
        start: int,
        end: int,
        batch_size: int,
        data: bytes,
    ) -> "RankSampler":
        sampler = cls(
            rank=rank,
            seed=0,
            start=start,
            end=end,
            batch_size=batch_size,
        )
        sampler.restore(data)
        return sampler
