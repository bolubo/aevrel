from __future__ import annotations

from dataclasses import dataclass

from .cdp import ChunkingPolicy


@dataclass(frozen=True)
class AdaptiveChunkingPolicy:
    """Choose a chunk strategy from the current RTO/data objective."""

    objective: str = "speed"
    cdc_policy: ChunkingPolicy = ChunkingPolicy.content_defined()

    def __post_init__(self) -> None:
        if self.objective not in {"speed", "bytes", "balanced"}:
            raise ValueError(f"unsupported performance objective: {self.objective}")

    def choose(
        self,
        *,
        total_bytes: int,
        target_ranks: int,
        changed_ranks: int,
    ) -> ChunkingPolicy:
        if self.objective == "speed":
            return ChunkingPolicy.file()
        if self.objective == "bytes":
            return self.cdc_policy
        if target_ranks < 1:
            return ChunkingPolicy.file()
        per_rank = total_bytes / target_ranks
        changed_ratio = changed_ranks / target_ranks
        if changed_ratio <= 0.10 and per_rank >= 1024 * 1024:
            return self.cdc_policy
        return ChunkingPolicy.file()
