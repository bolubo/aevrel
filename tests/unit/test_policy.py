from __future__ import annotations

from aevrel.cdp import ChunkingPolicy
from aevrel.policy import AdaptiveChunkingPolicy


def test_speed_objective_uses_file_chunks() -> None:
    policy = AdaptiveChunkingPolicy("speed")
    assert policy.choose(
        total_bytes=1024 * 1024,
        target_ranks=64,
        changed_ranks=64,
    ).algorithm == "file"


def test_bytes_objective_uses_cdc() -> None:
    policy = AdaptiveChunkingPolicy("bytes")
    assert policy.choose(
        total_bytes=1024 * 1024,
        target_ranks=64,
        changed_ranks=1,
    ).algorithm == "cdc"


def test_balanced_uses_cdc_for_small_large_rank_changes() -> None:
    policy = AdaptiveChunkingPolicy("balanced")
    decision = policy.choose(
        total_bytes=1024 * 1024 * 1024,
        target_ranks=64,
        changed_ranks=1,
    )
    assert decision.algorithm == "cdc"
    assert decision is not ChunkingPolicy.file()


def test_balanced_uses_file_for_many_or_small_changes() -> None:
    policy = AdaptiveChunkingPolicy("balanced")
    many = policy.choose(
        total_bytes=1024 * 1024 * 1024,
        target_ranks=64,
        changed_ranks=32,
    )
    small = policy.choose(
        total_bytes=1024 * 1024,
        target_ranks=64,
        changed_ranks=1,
    )
    assert many.algorithm == "file"
    assert small.algorithm == "file"
