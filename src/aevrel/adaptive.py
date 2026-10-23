from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit


class RateLimitTimeout(TimeoutError):
    """Raised when a request cannot obtain a token before its deadline."""


@dataclass
class RateSnapshot:
    rate: float
    burst: float
    tokens: float
    throttles: int
    successes: int
    errors: int


class AdaptiveTokenBucket:
    """Thread-safe token bucket with an AIMD-style adaptive rate.

    The limiter is intentionally transport-neutral. HTTP callers acquire one
    token per request; a 429 halves the rate, while successful requests
    gradually recover toward ``max_rate``.
    """

    def __init__(
        self,
        rate: float = 10.0,
        *,
        min_rate: float = 0.5,
        max_rate: float = 100.0,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if min_rate <= 0 or max_rate < min_rate:
            raise ValueError("invalid rate bounds")
        self._clock = clock
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._rate = min(max(rate, min_rate), max_rate)
        self._burst = max(1.0, burst if burst is not None else self._rate)
        self._tokens = self._burst
        self._updated = self._clock()
        self._throttles = 0
        self._successes = 0
        self._errors = 0
        self._condition = threading.Condition()

    def acquire(self, amount: float = 1.0, timeout: float | None = None) -> None:
        if amount <= 0:
            return
        deadline = None if timeout is None else self._clock() + timeout
        with self._condition:
            while True:
                self._refill_locked()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise RateLimitTimeout(
                            f"rate limit wait exceeded {timeout:.3f}s"
                        )
                    wait = min(remaining, (amount - self._tokens) / self._rate)
                else:
                    wait = (amount - self._tokens) / self._rate
                self._condition.wait(max(0.001, wait))

    def on_success(self) -> None:
        with self._condition:
            self._successes += 1
            self._rate = min(self._max_rate, self._rate * 1.02 + 0.01)
            self._burst = max(self._burst, self._rate)
            self._condition.notify_all()

    def on_throttle(self, retry_after: float | None = None) -> None:
        with self._condition:
            self._throttles += 1
            self._rate = max(self._min_rate, self._rate * 0.5)
            self._burst = max(1.0, min(self._burst, self._rate))
            self._tokens = min(self._tokens, self._burst)
            if retry_after is not None and retry_after > 0:
                self._tokens = 0.0
            self._condition.notify_all()

    def on_error(self) -> None:
        with self._condition:
            self._errors += 1
            self._condition.notify_all()

    def snapshot(self) -> RateSnapshot:
        with self._condition:
            self._refill_locked()
            return RateSnapshot(
                rate=self._rate,
                burst=self._burst,
                tokens=self._tokens,
                throttles=self._throttles,
                successes=self._successes,
                errors=self._errors,
            )

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(
            self._burst,
            self._tokens + elapsed * self._rate,
        )


class AdaptiveRateController(AdaptiveTokenBucket):
    """Explicit name for callers that want to manage a single limiter."""


class AdaptiveRateRegistry:
    """Keep one adaptive limiter per HTTP endpoint.

    A throttled source node should not reduce the rate of every other source.
    The key is ``scheme://host:port`` so separate agents and ports are
    independent, while all requests to the same agent share one bucket.
    """

    def __init__(
        self,
        rate: float = 10.0,
        *,
        min_rate: float = 0.5,
        max_rate: float = 100.0,
        burst: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._defaults = {
            "rate": rate,
            "min_rate": min_rate,
            "max_rate": max_rate,
            "burst": burst,
            "clock": clock,
        }
        self._lock = threading.Lock()
        self._limiters: dict[str, AdaptiveRateController] = {}

    def for_url(self, url: str) -> AdaptiveRateController:
        parsed = urlsplit(url)
        key = (
            f"{parsed.scheme}://{parsed.hostname or ''}:"
            f"{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        )
        with self._lock:
            limiter = self._limiters.get(key)
            if limiter is None:
                limiter = AdaptiveRateController(**self._defaults)
                self._limiters[key] = limiter
            return limiter

    def snapshot(self) -> dict[str, RateSnapshot]:
        with self._lock:
            limiters = list(self._limiters.items())
        return {key: limiter.snapshot() for key, limiter in limiters}

