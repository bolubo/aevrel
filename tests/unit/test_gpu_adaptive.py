from __future__ import annotations

import random

from aevrel.gpu_adaptive import AdaptiveRangeCodec
from aevrel.gpu_codec import IdentityCodec, ZlibCodec, decode_frame


def test_adaptive_codec_compresses_compressible_range_on_slow_wan() -> None:
    codec = AdaptiveRangeCodec(
        ZlibCodec(level=3),
        network_gbps=0.1,
        sample_bytes=16 * 1024,
        min_bytes=64 * 1024,
    )
    data = b"checkpoint-range-" * (128 * 1024)
    result = codec.encode(data)

    assert result.decision.enabled is True
    assert result.compressed is True
    assert result.decision.sample_ratio < 0.5
    assert decode_frame(
        result.frame,
        {"identity": IdentityCodec(), "zlib": ZlibCodec(level=3)},
    ) == data


def test_adaptive_codec_keeps_random_range_on_fast_lan() -> None:
    codec = AdaptiveRangeCodec(
        ZlibCodec(level=3),
        network_gbps=10.0,
        sample_bytes=16 * 1024,
        min_bytes=64 * 1024,
    )
    data = random.Random(7).randbytes(128 * 1024)
    result = codec.encode(data)

    assert result.decision.enabled is False
    assert result.compressed is False
    assert result.decision.reason
    assert decode_frame(
        result.frame,
        {"identity": IdentityCodec(), "zlib": ZlibCodec(level=3)},
    ) == data


def test_adaptive_codec_keeps_small_range_identity() -> None:
    codec = AdaptiveRangeCodec(
        ZlibCodec(level=3),
        network_gbps=1.0,
        min_bytes=64 * 1024,
    )
    result = codec.encode(b"x" * 1024)

    assert result.decision.enabled is False
    assert result.compressed is False
    assert "small" in result.decision.reason


def test_adaptive_codec_requires_known_network_bandwidth() -> None:
    codec = AdaptiveRangeCodec(
        ZlibCodec(level=3),
        network_gbps=None,
        min_bytes=64 * 1024,
    )
    result = codec.encode(b"x" * (128 * 1024))

    assert result.decision.enabled is False
    assert "bandwidth" in result.decision.reason


def test_adaptive_codec_samples_head_middle_and_tail() -> None:
    codec = AdaptiveRangeCodec(
        ZlibCodec(level=3),
        network_gbps=1.0,
        sample_bytes=12 * 1024,
        sample_count=3,
        min_bytes=32 * 1024,
    )
    data = b"A" * (32 * 1024) + random.Random(9).randbytes(32 * 1024) + b"B" * (32 * 1024)
    decision = codec.decide(data)

    assert decision.sample_bytes == 12 * 1024
    assert decision.sample_ratio < 1.0
