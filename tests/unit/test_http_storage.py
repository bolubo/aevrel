from __future__ import annotations

import pytest

from shardferry.storage.file import StorageError
from shardferry.storage.http import HttpStorage
from shardferry.storage.router import StorageRouter


class _Response:
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

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
