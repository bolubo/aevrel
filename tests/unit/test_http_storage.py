from __future__ import annotations

import pytest

from aevrel.adaptive import AdaptiveRateRegistry
from aevrel.storage.file import StorageError
from aevrel.storage.http import HttpRateLimitError, HttpStorage
from aevrel.storage.router import StorageRouter


class _Response:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data
        self.headers = {}

    def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        return None


def test_http_storage_retries_transient_request(monkeypatch) -> None:
    storage = HttpStorage(retries=2, backoff=0)
    calls = {"count": 0}

    def flaky_open(method, url):
        calls["count"] += 1
        if calls["count"] == 1:
            raise StorageError("temporary connection failure")
        return _Response(b"ok")

    monkeypatch.setattr(storage, "_open", flaky_open)
    assert storage.read_range("http://example.test/shard/w/0", 0, 2) == b"ok"
    assert calls["count"] == 2


def test_http_storage_does_not_retry_404(monkeypatch) -> None:
    storage = HttpStorage(retries=3, backoff=0)
    calls = {"count": 0}

    def not_found(method, url):
        calls["count"] += 1
        raise StorageError(f"HTTP 404: {url}")

    monkeypatch.setattr(storage, "_open", not_found)
    assert storage.exists("http://example.test/missing") is False
    assert calls["count"] == 1


def test_storage_router_propagates_source_token() -> None:
    router = StorageRouter(
        source_base="http://source.test",
        token="controller-token",
    )
    assert router.http.token == "controller-token"


def test_http_storage_honors_retry_after_hint(monkeypatch) -> None:
    from aevrel.adaptive import AdaptiveRateController

    limiter = AdaptiveRateController(rate=10, burst=10)
    storage = HttpStorage(retries=1, backoff=0, rate_controller=limiter)
    sleeps: list[float] = []
    calls = {"count": 0}

    def throttled_then_ok(method, url):
        calls["count"] += 1
        if calls["count"] == 1:
            raise HttpRateLimitError(
                "HTTP 429: too many requests",
                retry_after=0.75,
            )
        return _Response(b"ok")

    monkeypatch.setattr(storage, "_open", throttled_then_ok)
    monkeypatch.setattr(
        "aevrel.storage.http.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    assert storage.read_range("http://example.test/shard/w/0", 0, 2) == b"ok"
    assert sleeps == [0.75]
    assert limiter.snapshot().throttles == 1


def test_http_storage_uses_per_url_rate_limiter(monkeypatch) -> None:
    registry = AdaptiveRateRegistry(rate=10)
    storage = HttpStorage(rate_registry=registry)
    monkeypatch.setattr(
        storage,
        "_open",
        lambda method, url: _Response(b"ok"),
    )
    storage.read_range("http://source-a:9000/w/0", 0, 2)
    storage.read_range("http://source-b:9000/w/0", 0, 2)
    assert set(registry.snapshot()) == {
        "http://source-a:9000",
        "http://source-b:9000",
    }
