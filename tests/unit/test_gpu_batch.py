from __future__ import annotations

import random

import pytest

from shardferry.gpu_adaptive import AdaptiveRangeCodec
from shardferry.gpu_batch import (
    AdaptiveBatchCodec,
    BatchCodecError,
    decode_batch,
)
from shardferry.gpu_codec import (
    CodecError,
    IdentityCodec,
    ZlibCodec,
)


def _codec(network_gbps: float = 0.01) -> AdaptiveBatchCodec:
    return AdaptiveBatchCodec(
        AdaptiveRangeCodec(
            ZlibCodec(level=3),
            network_gbps=network_gbps,
            min_bytes=1024,
            sample_bytes=16 * 1024,
        ),
        max_ranges=8,
        max_bytes=8 * 1024 * 1024,
    )


def test_batch_roundtrip_compresses_all_ranges() -> None:
    codec = _codec()
    chunks = [b"checkpoint-range-" * 4096 for _ in range(4)]
    result = codec.encode(chunks)

    assert result.compressed is True
    assert result.decision.enabled is True
    assert result.lengths == tuple(len(chunk) for chunk in chunks)
    assert codec.decode(result.frame) == chunks


def test_batch_identity_for_random_ranges() -> None:
    codec = _codec()
    chunks = [
        random.Random(index).randbytes(16 * 1024)
        for index in range(4)
    ]
    result = codec.encode(chunks)

    assert result.compressed is False
    assert result.decision.enabled is False
    assert codec.decode(result.frame) == chunks


def test_batch_rejects_empty_ranges() -> None:
    codec = _codec()
    with pytest.raises(BatchCodecError, match="empty"):
        codec.encode([])


def test_batch_rejects_too_many_ranges() -> None:
    codec = _codec()
    chunks = [b"x" * 1024 for _ in range(9)]
    with pytest.raises(BatchCodecError, match="too many"):
        codec.encode(chunks)


def test_batch_rejects_oversized_batch() -> None:
    codec = AdaptiveBatchCodec(
        AdaptiveRangeCodec(
            ZlibCodec(level=3),
            network_gbps=0.01,
            min_bytes=1024,
            sample_bytes=16 * 1024,
        ),
        max_ranges=8,
        max_bytes=2048,
    )
    with pytest.raises(BatchCodecError, match="too large"):
        codec.encode([b"x" * 1024, b"y" * 1024, b"z" * 1024])


def test_batch_decode_rejects_truncated_frame() -> None:
    codec = _codec()
    result = codec.encode([b"x" * 1024, b"y" * 1024])
    with pytest.raises(BatchCodecError, match="frame"):
        decode_batch(
            result.frame[:-1],
            {
                "identity": IdentityCodec(),
                "zlib": ZlibCodec(level=3),
            },
        )


def test_batch_decode_rejects_length_mismatch() -> None:
    codec = _codec()
    result = codec.encode([b"x" * 1024, b"y" * 1024])
    frame = bytearray(result.frame)
    # The first length follows the fixed SFB1 header.
    frame[29] ^= 0x01
    with pytest.raises((BatchCodecError, CodecError)):
        decode_batch(
            bytes(frame),
            {
                "identity": IdentityCodec(),
                "zlib": ZlibCodec(level=3),
            },
        )
