"""RNG streams that move with data, layers, and micro-batches."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..state import NumpyRandomProvider


@dataclass(frozen=True)
class RNGStreamKey:
    scope: str
    rank: int
    layer: int | None = None
    microbatch: int | None = None

    def __post_init__(self) -> None:
        if self.scope not in {"data", "layer", "microbatch"}:
            raise ValueError("unknown RNG stream scope")
        if self.rank < 0:
            raise ValueError("rank must be non-negative")


class RNGStream:
    def __init__(self, key: RNGStreamKey, seed: int = 0) -> None:
        self.key = key
        self.rng = np.random.RandomState(seed)
        self.provider = NumpyRandomProvider(self.rng)

    def capture(self) -> bytes:
        return self.provider.capture()

    def restore(self, data: bytes) -> None:
        self.provider.restore(data)

    def draw(self, size: int = 1) -> tuple[int, ...]:
        if size < 1:
            raise ValueError("size must be positive")
        values = self.rng.randint(0, 2**31 - 1, size=size)
        return tuple(int(value) for value in values)


class RNGStreamRegistry:
    def __init__(self) -> None:
        self._streams: dict[RNGStreamKey, RNGStream] = {}

    def add(self, stream: RNGStream) -> None:
        if stream.key in self._streams:
            raise ValueError(f"duplicate RNG stream {stream.key}")
        self._streams[stream.key] = stream

    def get(self, key: RNGStreamKey) -> RNGStream:
        try:
            return self._streams[key]
        except KeyError as exc:
            raise KeyError(f"unknown RNG stream {key}") from exc

    def capture(self) -> dict[RNGStreamKey, bytes]:
        return {
            key: stream.capture()
            for key, stream in self._streams.items()
        }

    def restore(
        self,
        states: dict[RNGStreamKey, bytes],
    ) -> None:
        for key, data in states.items():
            stream = self._streams.get(key)
            if stream is None:
                stream = RNGStream(key)
                self._streams[key] = stream
            stream.restore(data)

    def reshard(
        self,
        mapping: dict[RNGStreamKey, RNGStreamKey],
    ) -> "RNGStreamRegistry":
        """Move stream states from old keys to new keys.

        Unmapped streams are copied unchanged. The source registry is not
        mutated.
        """
        target = RNGStreamRegistry()
        for old_key, new_key in mapping.items():
            source = self.get(old_key)
            moved = RNGStream(new_key)
            moved.restore(source.capture())
            target.add(moved)
        for key, stream in self._streams.items():
            if key in mapping:
                continue
            copied = RNGStream(key)
            copied.restore(stream.capture())
            target.add(copied)
        return target
