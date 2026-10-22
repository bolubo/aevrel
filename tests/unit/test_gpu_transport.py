from __future__ import annotations

import hashlib
import random

import pytest

from shardferry.gpu_adaptive import AdaptiveRangeCodec
from shardferry.gpu_codec import ZlibCodec
from shardferry.gpu_transport import (
    RANGE_ENCODING,
    RANGE_ENCODING_HEADER,
    RANGE_RAW_LENGTH_HEADER,
    RangeCodec,
    encode_response,
)
from shardferry.storage.file import StorageError
from shardferry.storage.http import HttpStorage
from shardferry.storage.router import StorageRouter


def _range_codec() -> RangeCodec:
    return RangeCodec(
        AdaptiveRangeCodec(
            ZlibCodec(level=3),
            network_gbps=0.01,
            min_bytes=1024,
            sample_bytes=16 * 1024,
        )
    )


def _checksum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Response:
    def __init__(self, data: bytes, headers: dict[str, str]) -> None:
        self._data = data
        self.headers = headers

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return


def test_range_codec_roundtrip_and_identity_fallback() -> None:
    codec = _range_codec()
    compressible = b"checkpoint-" * (64 * 1024)
    result = codec.encode(compressible)
    assert result.compressed is True
    assert codec.decode(result.frame) == compressible

    random_data = random.Random(7).randbytes(64 * 1024)
    identity = codec.encode(random_data)
    assert identity.compressed is False
    assert codec.decode(identity.frame) == random_data


def test_encode_response_negotiates_and_preserves_raw_length() -> None:
    codec = _range_codec()
    raw = b"checkpoint-range-" * (64 * 1024)
    response = encode_response(raw, codec, RANGE_ENCODING)

    assert response.encoding == RANGE_ENCODING
    assert response.raw_length == len(raw)
    assert response.raw_checksum == _checksum(raw)
    assert codec.decode(response.data) == raw

    identity = encode_response(raw, codec, "")
    assert identity.encoding is None
    assert identity.data == raw

    head = encode_response(raw, codec, RANGE_ENCODING, head=True)
    assert head.encoding is None
    assert head.data == raw


def test_http_storage_decodes_negotiated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"checkpoint-range-" * (64 * 1024)
    codec = _range_codec()
    encoded = codec.encode(raw)
    response = _Response(
        encoded.frame,
        {
            RANGE_ENCODING_HEADER: RANGE_ENCODING,
            RANGE_RAW_LENGTH_HEADER: str(len(raw)),
            "X-Range-SHA256": _checksum(raw),
        },
    )
    storage = HttpStorage(base_url="http://source", range_codec=codec)
    monkeypatch.setattr(storage, "_request", lambda *_args: response)

    assert storage.read_range("v1/shards/test", 0, len(raw)) == raw


def test_http_storage_keeps_identity_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"plain-shard-data" * 1024
    response = _Response(raw, {"X-Range-SHA256": _checksum(raw)})
    storage = HttpStorage(base_url="http://source", range_codec=_range_codec())
    monkeypatch.setattr(storage, "_request", lambda *_args: response)

    assert storage.read_range("v1/shards/test", 0, len(raw)) == raw


def test_http_storage_rejects_encoded_response_without_decoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"encoded-without-client-codec" * 1024
    encoded = _range_codec().encode(raw)
    response = _Response(
        encoded.frame,
        {
            RANGE_ENCODING_HEADER: RANGE_ENCODING,
            RANGE_RAW_LENGTH_HEADER: str(len(raw)),
            "X-Range-SHA256": _checksum(raw),
        },
    )
    storage = HttpStorage(base_url="http://source")
    monkeypatch.setattr(storage, "_request", lambda *_args: response)

    with pytest.raises(StorageError, match="encoding"):
        storage.read_range("v1/shards/test", 0, len(raw))


def test_storage_router_reads_range_codec_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHARDFERRY_GPU_CODEC", "zlib")
    monkeypatch.setenv("SHARDFERRY_GPU_CODEC_NETWORK_GBPS", "0.01")
    router = StorageRouter(source_base="http://source")

    assert router.range_codec is not None
    assert router.http.range_codec is router.range_codec
