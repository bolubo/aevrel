from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from .gpu_adaptive import AdaptiveRangeCodec, AdaptiveResult
from .gpu_codec import make_codec


RANGE_ACCEPT_HEADER = "X-Shard-Accept-Encoding"
RANGE_ENCODING_HEADER = "X-Shard-Encoding"
RANGE_RAW_LENGTH_HEADER = "X-Shard-Raw-Length"
RANGE_ENCODING = "sfg1"


class RangeCodec:
    def __init__(self, adaptive: AdaptiveRangeCodec) -> None:
        self.adaptive = adaptive

    def encode(self, data: bytes) -> AdaptiveResult:
        return self.adaptive.encode(data)

    def decode(self, frame: bytes) -> bytes:
        return self.adaptive.decode(frame)


@dataclass(frozen=True)
class RangeResponse:
    data: bytes
    encoding: str | None
    raw_length: int
    raw_checksum: str


def encode_response(
    raw_data: bytes,
    codec: RangeCodec | None,
    accept_encoding: str,
    *,
    head: bool = False,
    raw_checksum: str | None = None,
) -> RangeResponse:
    checksum = raw_checksum or (
        "sha256:" + hashlib.sha256(raw_data).hexdigest()
    )
    if head or codec is None or RANGE_ENCODING not in accept_encoding:
        return RangeResponse(raw_data, None, len(raw_data), checksum)
    try:
        result = codec.encode(raw_data)
    except Exception:
        return RangeResponse(raw_data, None, len(raw_data), checksum)
    if not result.compressed:
        return RangeResponse(raw_data, None, len(raw_data), checksum)
    return RangeResponse(result.frame, RANGE_ENCODING, len(raw_data), checksum)


def make_range_codec_from_env() -> RangeCodec | None:
    name = os.environ.get("SHARDFERRY_GPU_CODEC", "").strip().lower()
    if not name or name in {"off", "identity"}:
        return None
    network = os.environ.get(
        "SHARDFERRY_GPU_CODEC_NETWORK_GBPS",
        "",
    ).strip()
    if not network:
        raise ValueError(
            "SHARDFERRY_GPU_CODEC_NETWORK_GBPS is required when "
            "SHARDFERRY_GPU_CODEC is enabled"
        )
    try:
        network_gbps = float(network)
    except ValueError as exc:
        raise ValueError(
            "SHARDFERRY_GPU_CODEC_NETWORK_GBPS must be a number"
        ) from exc
    if network_gbps <= 0:
        raise ValueError(
            "SHARDFERRY_GPU_CODEC_NETWORK_GBPS must be positive"
        )
    return RangeCodec(
        AdaptiveRangeCodec(
            make_codec(name),
            network_gbps=network_gbps,
        )
    )
