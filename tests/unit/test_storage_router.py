from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from aevrel.adaptive import AdaptiveRateRegistry
from aevrel.storage.file import StorageError
from aevrel.storage.router import StorageRouter


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch) -> None:
    # The router reads these at construction time; do not depend on ambient env.
    monkeypatch.delenv("AEVREL_RATE_LIMIT_RPS", raising=False)
    monkeypatch.delenv("AEVREL_GPU_CODEC", raising=False)
    monkeypatch.delenv("AEVREL_GPU_CODEC_NETWORK_GBPS", raising=False)


class _Recorder:
    """Stand-in backend that records calls instead of touching disk or network."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.base_url = None
        self.calls: list[tuple] = []

    def read_range(self, uri, offset, size):
        self.calls.append(("read_range", uri, offset, size))
        return self.name.encode()

    def exists(self, uri):
        self.calls.append(("exists", uri))
        return True

    def size(self, uri):
        self.calls.append(("size", uri))
        return 7

    def checksum(self, uri):
        self.calls.append(("checksum", uri))
        return f"sha256:{self.name}"

    def read_batch(self, requests):
        self.calls.append(("read_batch", list(requests)))
        return [self.name.encode() for _ in requests]


def _router_with_recorders(**kwargs) -> tuple[StorageRouter, dict[str, _Recorder]]:
    router = StorageRouter(**kwargs)
    recorders = {
        "file": _Recorder("file"),
        "dcpzip": _Recorder("dcpzip"),
        "http": _Recorder("http"),
    }
    router.file = recorders["file"]
    router.dcpzip = recorders["dcpzip"]
    router.http = recorders["http"]
    return router, recorders


def _exercise(router: StorageRouter, uri: str) -> None:
    assert router.read_range(uri, 1, 2)
    assert router.exists(uri) is True
    assert router.size(uri) == 7
    assert router.checksum(uri).startswith("sha256:")


@pytest.mark.parametrize(
    ("uri", "owner"),
    [
        ("file:///tmp/data.bin", "file"),
        ("dcpzip:///tmp/shard.distcp?name=a&offset=0&size=1&compression=0", "dcpzip"),
        ("http://agent.example/data.bin", "http"),
        ("https://agent.example/data.bin", "http"),
    ],
)
def test_each_scheme_reaches_only_its_own_backend(uri, owner) -> None:
    router, recorders = _router_with_recorders()
    _exercise(router, uri)
    for name, recorder in recorders.items():
        if name == owner:
            assert [call[0] for call in recorder.calls] == [
                "read_range",
                "exists",
                "size",
                "checksum",
            ]
            assert all(call[1] == uri for call in recorder.calls)
        else:
            assert recorder.calls == []


def test_file_uri_stays_local_even_when_a_source_base_is_configured() -> None:
    router, recorders = _router_with_recorders(source_base="http://agent.example")
    _exercise(router, "file:///tmp/data.bin")
    assert recorders["http"].calls == []
    assert len(recorders["file"].calls) == 4


def test_read_range_passes_offset_and_size_through() -> None:
    router, recorders = _router_with_recorders()
    router.read_range("file:///tmp/data.bin", 5, 9)
    assert recorders["file"].calls == [("read_range", "file:///tmp/data.bin", 5, 9)]


@pytest.mark.parametrize(
    "uri",
    ["ftp://host/data.bin", "s3://bucket/key", "relative/data.bin", ""],
)
def test_unknown_scheme_without_source_base_raises(uri) -> None:
    router, recorders = _router_with_recorders()
    operations = [
        lambda: router.read_range(uri, 0, 1),
        lambda: router.exists(uri),
        lambda: router.size(uri),
        lambda: router.checksum(uri),
    ]
    for operation in operations:
        with pytest.raises(StorageError) as excinfo:
            operation()
        assert str(excinfo.value) == f"unsupported storage URI: {uri}"
    # Nothing may be guessed: no backend saw the call.
    assert all(recorder.calls == [] for recorder in recorders.values())


def test_relative_reference_resolves_against_the_configured_source_base() -> None:
    router, recorders = _router_with_recorders(source_base="http://agent.example/")
    recorders["http"].base_url = "http://agent.example"
    assert router.read_range("relative/data.bin", 0, 1) == b"http"
    assert recorders["http"].calls == [("read_range", "relative/data.bin", 0, 1)]
    assert recorders["file"].calls == []
    assert recorders["dcpzip"].calls == []


@pytest.mark.parametrize(
    "uri",
    ["ftp://host/data.bin", "s3://bucket/key", "//host/data.bin", ""],
)
def test_explicit_unsupported_scheme_is_refused_even_with_a_source_base(uri) -> None:
    """A configured base resolves relative references, nothing else.

    Handing s3:// to the HTTP backend turns a one-line mistake into a network
    error somewhere else, which is the whole point of this dispatch.
    """
    router, recorders = _router_with_recorders(source_base="http://agent.example/")
    recorders["http"].base_url = "http://agent.example"
    operations = [
        lambda: router.read_range(uri, 0, 1),
        lambda: router.exists(uri),
        lambda: router.size(uri),
        lambda: router.checksum(uri),
    ]
    for operation in operations:
        with pytest.raises(StorageError) as excinfo:
            operation()
        assert str(excinfo.value) == f"unsupported storage URI: {uri}"
    # Nothing may be guessed: no backend saw the call.
    assert all(recorder.calls == [] for recorder in recorders.values())


def test_file_scheme_routes_to_real_local_storage(tmp_path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"abcdef")
    router = StorageRouter()
    uri = path.as_uri()
    assert router.exists(uri) is True
    assert router.size(uri) == 6
    assert router.read_range(uri, 1, 3) == b"bcd"
    assert router.checksum(uri) == "sha256:" + hashlib.sha256(b"abcdef").hexdigest()
    assert router.exists((tmp_path / "missing.bin").as_uri()) is False


def test_file_errors_propagate_as_storage_error(tmp_path) -> None:
    router = StorageRouter()
    missing = (tmp_path / "missing.bin").as_uri()
    with pytest.raises(StorageError, match="could not read"):
        router.read_range(missing, 0, 1)
    with pytest.raises(StorageError, match="could not stat"):
        router.size(missing)
    with pytest.raises(StorageError, match="could not hash"):
        router.checksum(missing)


def test_no_rate_registry_without_env_or_arguments() -> None:
    router = StorageRouter()
    assert router.http.rate_registry is None
    assert router.http.rate_controller is None


def test_rate_limit_env_creates_a_registry(monkeypatch) -> None:
    monkeypatch.setenv("AEVREL_RATE_LIMIT_RPS", "5")
    router = StorageRouter()
    assert isinstance(router.http.rate_registry, AdaptiveRateRegistry)


def test_invalid_rate_limit_env_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("AEVREL_RATE_LIMIT_RPS", "not-a-number")
    router = StorageRouter()
    assert router.http.rate_registry is None


def test_explicit_rate_arguments_win_over_env(monkeypatch) -> None:
    monkeypatch.setenv("AEVREL_RATE_LIMIT_RPS", "5")
    controller = object()
    router = StorageRouter(rate_controller=controller)
    assert router.http.rate_controller is controller
    assert router.http.rate_registry is None

    registry = AdaptiveRateRegistry(rate=2.0)
    router = StorageRouter(rate_registry=registry)
    assert router.http.rate_registry is registry


def test_batch_is_disabled_without_a_range_codec() -> None:
    router = StorageRouter()
    assert router.range_codec is None
    assert router.batch_enabled is False
    for requests in ([], [("http://agent.example/a", 0, 1)]):
        with pytest.raises(StorageError) as excinfo:
            router.read_batch(requests)
        assert str(excinfo.value) == "batch reads are not configured"


def _batch_router():
    codec = SimpleNamespace(adaptive=object())
    router, recorders = _router_with_recorders(range_codec=codec)
    # The recorder replaced the real HttpStorage, so mirror its batch flag.
    recorders["http"].batch_enabled = True
    return router, recorders


def test_batch_enabled_needs_codec_and_http_batch_support() -> None:
    codec = SimpleNamespace(adaptive=object())
    router = StorageRouter(range_codec=codec)
    assert router.range_codec is codec
    assert router.batch_enabled is True


def test_empty_batch_returns_empty_list_without_touching_http() -> None:
    router, recorders = _batch_router()
    assert router.read_batch([]) == []
    assert recorders["http"].calls == []


def test_batch_rejects_non_http_uris() -> None:
    router, recorders = _batch_router()
    for bad in (
        "file:///tmp/data.bin",
        "dcpzip:///tmp/shard.distcp?name=a&offset=0&size=1&compression=0",
        "relative/data.bin",
    ):
        requests = [("http://agent.example/a", 0, 1), (bad, 0, 1)]
        with pytest.raises(StorageError) as excinfo:
            router.read_batch(requests)
        assert str(excinfo.value) == "batch reads require HTTP source URIs"
    assert recorders["http"].calls == []


def test_batch_of_http_uris_is_delegated_to_http() -> None:
    router, recorders = _batch_router()
    requests = [
        ("http://agent.example/a", 0, 4),
        ("https://agent.example/b", 4, 8),
    ]
    assert router.read_batch(requests) == [b"http", b"http"]
    assert recorders["http"].calls == [("read_batch", requests)]
