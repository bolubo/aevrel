from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .gpu_codec import (
    Codec,
    CodecError,
    FRAME_HEADER,
    IdentityCodec,
    decode_frame,
    encode_frame,
)


@dataclass(frozen=True)
class AdaptiveDecision:
    enabled: bool
    reason: str
    sample_ratio: float
    sample_bytes: int
    projected_gain_seconds: float
    estimated_frame_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "sample_ratio": self.sample_ratio,
            "sample_bytes": self.sample_bytes,
            "projected_gain_seconds": self.projected_gain_seconds,
            "estimated_frame_bytes": self.estimated_frame_bytes,
        }


@dataclass(frozen=True)
class AdaptiveResult:
    frame: bytes
    decision: AdaptiveDecision
    compressed: bool


class AdaptiveRangeCodec:
    def __init__(
        self,
        codec: Codec,
        *,
        network_gbps: float | None,
        sample_bytes: int = 1024 * 1024,
        sample_count: int = 3,
        min_bytes: int = 1024 * 1024,
        max_ratio: float = 0.95,
        min_gain_seconds: float = 0.0,
        min_gain_fraction: float = 0.1,
        overhead_safety: float = 1.1,
        compress_mib_s: float | None = None,
        decompress_mib_s: float | None = None,
    ) -> None:
        if sample_bytes < 1:
            raise ValueError("sample_bytes must be positive")
        if sample_count < 1:
            raise ValueError("sample_count must be positive")
        if min_bytes < 0:
            raise ValueError("min_bytes must be non-negative")
        if max_ratio <= 0 or max_ratio > 1:
            raise ValueError("max_ratio must be in (0, 1]")
        if min_gain_seconds < 0:
            raise ValueError("min_gain_seconds must be non-negative")
        if min_gain_fraction < 0 or min_gain_fraction > 1:
            raise ValueError("min_gain_fraction must be in [0, 1]")
        if overhead_safety < 1:
            raise ValueError("overhead_safety must be at least 1")
        if compress_mib_s is not None and compress_mib_s <= 0:
            raise ValueError("compress_mib_s must be positive")
        if decompress_mib_s is not None and decompress_mib_s <= 0:
            raise ValueError("decompress_mib_s must be positive")
        self.codec = codec
        self.network_gbps = network_gbps
        self.sample_bytes = sample_bytes
        self.sample_count = sample_count
        self.min_bytes = min_bytes
        self.max_ratio = max_ratio
        self.min_gain_seconds = min_gain_seconds
        self.min_gain_fraction = min_gain_fraction
        self.overhead_safety = overhead_safety
        self.compress_mib_s = compress_mib_s
        self.decompress_mib_s = decompress_mib_s

    def _sample(self, data: bytes) -> bytes:
        if len(data) <= self.sample_bytes:
            return data
        window = max(1, self.sample_bytes // self.sample_count)
        positions = {
            0,
            max(0, (len(data) - window) // 2),
            max(0, len(data) - window),
        }
        sample = b"".join(
            data[offset : offset + window] for offset in sorted(positions)
        )
        return sample[: self.sample_bytes]

    def decide(self, data: bytes) -> AdaptiveDecision:
        raw = bytes(data)
        if len(raw) < self.min_bytes:
            return AdaptiveDecision(
                False,
                f"range is too small for adaptive compression: "
                f"{len(raw)} < {self.min_bytes}",
                1.0,
                0,
                0.0,
                0,
            )
        if self.network_gbps is None or self.network_gbps <= 0:
            return AdaptiveDecision(
                False,
                "network bandwidth is required",
                1.0,
                0,
                0.0,
                0,
            )
        if hasattr(self.codec, "available") and not self.codec.available():
            return AdaptiveDecision(
                False,
                f"codec is unavailable: {self.codec.name}",
                1.0,
                0,
                0.0,
                0,
            )
        sample = self._sample(raw)
        try:
            started = time.perf_counter()
            encoded_sample = self.codec.encode(sample)
            sample_encode_seconds = time.perf_counter() - started
            started = time.perf_counter()
            decoded_sample = self.codec.decode(encoded_sample)
            sample_decode_seconds = time.perf_counter() - started
        except CodecError as exc:
            return AdaptiveDecision(
                False,
                f"codec sample failed: {exc}",
                1.0,
                len(sample),
                0.0,
                0,
            )
        if decoded_sample != sample:
            return AdaptiveDecision(
                False,
                "codec sample round-trip mismatch",
                1.0,
                len(sample),
                0.0,
                0,
            )
        sample_ratio = len(encoded_sample) / len(sample)
        if sample_ratio >= self.max_ratio:
            return AdaptiveDecision(
                False,
                f"sample compression ratio {sample_ratio:.3f} is not below "
                f"{self.max_ratio:.3f}",
                sample_ratio,
                len(sample),
                0.0,
                0,
            )
        scale = len(raw) / len(sample)
        estimated_frame_bytes = (
            int(len(raw) * sample_ratio)
            + FRAME_HEADER.size
            + len(self.codec.name.encode("utf-8"))
        )
        bytes_per_second = self.network_gbps * 1_000_000_000 / 8
        raw_transfer_seconds = len(raw) / bytes_per_second
        compressed_transfer_seconds = estimated_frame_bytes / bytes_per_second
        if (
            self.compress_mib_s is not None
            and self.decompress_mib_s is not None
        ):
            full_seconds = (
                len(raw) / (1024**2) / self.compress_mib_s
                + len(raw) / (1024**2) / self.decompress_mib_s
            )
            estimated_overhead = (
                sample_encode_seconds
                + sample_decode_seconds
                + full_seconds * self.overhead_safety
            )
        else:
            estimated_overhead = (
                sample_encode_seconds
                + sample_decode_seconds
                + (
                    (sample_encode_seconds + sample_decode_seconds)
                    * scale
                    * self.overhead_safety
                )
            )
        gain = raw_transfer_seconds - (
            compressed_transfer_seconds + estimated_overhead
        )
        threshold = max(
            self.min_gain_seconds,
            raw_transfer_seconds * self.min_gain_fraction,
        )
        if gain <= threshold:
            return AdaptiveDecision(
                False,
                f"projected gain {gain:.3f}s is not above "
                f"{threshold:.3f}s",
                sample_ratio,
                len(sample),
                gain,
                estimated_frame_bytes,
            )
        return AdaptiveDecision(
            True,
            f"projected gain {gain:.3f}s exceeds "
            f"{threshold:.3f}s",
            sample_ratio,
            len(sample),
            gain,
            estimated_frame_bytes,
        )

    def encode(self, data: bytes) -> AdaptiveResult:
        raw = bytes(data)
        decision = self.decide(raw)
        selected = self.codec if decision.enabled else IdentityCodec()
        return AdaptiveResult(
            frame=encode_frame(selected, raw),
            decision=decision,
            compressed=decision.enabled,
        )

    def decode(self, frame: bytes) -> bytes:
        return decode_frame(
            frame,
            {
                "identity": IdentityCodec(),
                self.codec.name: self.codec,
            },
        )
