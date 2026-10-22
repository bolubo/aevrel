from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Mapping, Sequence

from .gpu_adaptive import AdaptiveDecision, AdaptiveRangeCodec, AdaptiveResult
from .gpu_codec import (
    Codec,
    CodecError,
    IdentityCodec,
    decode_frame,
)


BATCH_MAGIC = b"SFB1"
BATCH_VERSION = 1
BATCH_HEADER = struct.Struct(">4sBQQQ")
BATCH_ENCODING = "sfb1"


class BatchCodecError(CodecError):
    """Raised when a batch frame is invalid."""


@dataclass(frozen=True)
class BatchResult:
    frame: bytes
    decision: AdaptiveDecision
    compressed: bool
    lengths: tuple[int, ...]


class AdaptiveBatchCodec:
    def __init__(
        self,
        adaptive: AdaptiveRangeCodec,
        *,
        max_ranges: int = 32,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_ranges < 1:
            raise ValueError("max_ranges must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.adaptive = adaptive
        self.max_ranges = max_ranges
        self.max_bytes = max_bytes

    def encode(self, chunks: Sequence[bytes]) -> BatchResult:
        values = [bytes(chunk) for chunk in chunks]
        if not values:
            raise BatchCodecError(
                "empty batch: at least one range is required"
            )
        if len(values) > self.max_ranges:
            raise BatchCodecError(
                f"too many ranges in batch: {len(values)} > {self.max_ranges}"
            )
        lengths = tuple(len(value) for value in values)
        total = sum(lengths)
        if total > self.max_bytes:
            raise BatchCodecError(
                f"batch is too large: {total} > {self.max_bytes}"
            )
        combined = b"".join(values)
        encoded = self.adaptive.encode(combined)
        header = BATCH_HEADER.pack(
            BATCH_MAGIC,
            BATCH_VERSION,
            len(lengths),
            total,
            len(encoded.frame),
        )
        frame = (
            header
            + b"".join(struct.pack(">Q", length) for length in lengths)
            + encoded.frame
        )
        return BatchResult(
            frame=frame,
            decision=encoded.decision,
            compressed=encoded.compressed,
            lengths=lengths,
        )

    def decode(self, frame: bytes) -> list[bytes]:
        return decode_batch(
            frame,
            {
                "identity": IdentityCodec(),
                self.adaptive.codec.name: self.adaptive.codec,
            },
        )


def decode_batch(
    frame: bytes,
    codecs: Mapping[str, Codec],
    *,
    max_ranges: int = 4096,
) -> list[bytes]:
    raw_frame = bytes(frame)
    if len(raw_frame) < BATCH_HEADER.size:
        raise BatchCodecError("truncated batch frame")
    magic, version, count, total_raw, inner_length = BATCH_HEADER.unpack_from(
        raw_frame
    )
    if magic != BATCH_MAGIC:
        raise BatchCodecError("invalid batch frame magic")
    if version != BATCH_VERSION:
        raise BatchCodecError(f"unsupported batch frame version: {version}")
    if count < 1 or count > max_ranges:
        raise BatchCodecError(f"invalid batch range count: {count}")
    lengths_start = BATCH_HEADER.size
    lengths_end = lengths_start + count * 8
    expected_size = lengths_end + inner_length
    if len(raw_frame) != expected_size:
        raise BatchCodecError(
            f"invalid batch frame size: {len(raw_frame)} != {expected_size}"
        )
    lengths = tuple(
        struct.unpack_from(">Q", raw_frame, lengths_start + index * 8)[0]
        for index in range(count)
    )
    if sum(lengths) != total_raw:
        raise BatchCodecError(
            f"batch lengths sum {sum(lengths)} != raw size {total_raw}"
        )
    inner = raw_frame[lengths_end:]
    decoded = decode_frame(inner, codecs)
    if len(decoded) != total_raw:
        raise BatchCodecError(
            f"decoded batch size {len(decoded)} != raw size {total_raw}"
        )
    result = []
    offset = 0
    for length in lengths:
        result.append(decoded[offset : offset + length])
        offset += length
    return result
