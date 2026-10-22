from __future__ import annotations

import pytest

from shardferry.adaptive import (
    AdaptiveRateController,
    AdaptiveRateRegistry,
    RateLimitTimeout,
)


def test_token_bucket_refills_and_times_out() -> None:
    now = [0.0]
    limiter = AdaptiveRateController(
        rate=2,
        min_rate=0.5,
        max_rate=4,
        burst=2,
        clock=lambda: now[0],
    )
    limiter.acquire(2)
    with pytest.raises(RateLimitTimeout):
        limiter.acquire(1, timeout=0)
    now[0] = 0.5
    limiter.acquire(1, timeout=0.01)


def test_throttle_reduces_rate_and_success_recovers() -> None:
    limiter = AdaptiveRateController(rate=10, min_rate=1, max_rate=20, burst=10)
    before = limiter.snapshot().rate
    limiter.on_throttle()
    after = limiter.snapshot().rate
    assert after == before / 2
    for _ in range(20):
        limiter.on_success()
    assert limiter.snapshot().rate > after
    assert limiter.snapshot().successes == 20


def test_rate_registry_isolates_throttled_endpoints() -> None:
    registry = AdaptiveRateRegistry(rate=10, min_rate=1, max_rate=20)
    first = registry.for_url("http://source-a:9000/shard")
    second = registry.for_url("http://source-b:9000/shard")
    first.on_throttle()
    assert first.snapshot().rate < second.snapshot().rate
    assert registry.for_url("http://source-a:9000/other") is first
    assert set(registry.snapshot()) == {
        "http://source-a:9000",
        "http://source-b:9000",
    }
