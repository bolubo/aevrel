from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CodecDecision:
    enabled: bool
    reason: str
    ratio: float
    projected_gain_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "ratio": self.ratio,
            "projected_gain_seconds": self.projected_gain_seconds,
        }


def decide_codec(
    *,
    raw_bytes: int,
    encoded_bytes: int,
    compress_seconds: float,
    decompress_seconds: float,
    network_gbps: float | None,
    min_gain_seconds: float = 0.05,
    max_ratio: float = 0.95,
) -> CodecDecision:
    if raw_bytes <= 0:
        return CodecDecision(False, "empty payload", 0.0, 0.0)
    if encoded_bytes < 0 or compress_seconds < 0 or decompress_seconds < 0:
        raise ValueError("codec measurements must be non-negative")
    if network_gbps is None or network_gbps <= 0:
        return CodecDecision(
            False,
            "network bandwidth is required",
            encoded_bytes / raw_bytes,
            0.0,
        )
    ratio = encoded_bytes / raw_bytes
    if ratio >= max_ratio:
        return CodecDecision(
            False,
            f"compression ratio {ratio:.3f} is not below {max_ratio:.3f}",
            ratio,
            0.0,
        )
    bytes_per_second = network_gbps * 1_000_000_000 / 8
    raw_seconds = raw_bytes / bytes_per_second
    encoded_seconds = encoded_bytes / bytes_per_second
    gain = raw_seconds - (
        encoded_seconds + compress_seconds + decompress_seconds
    )
    if gain <= min_gain_seconds:
        return CodecDecision(
            False,
            f"projected gain {gain:.3f}s is not above {min_gain_seconds:.3f}s",
            ratio,
            gain,
        )
    return CodecDecision(
        True,
        f"projected gain {gain:.3f}s exceeds {min_gain_seconds:.3f}s",
        ratio,
        gain,
    )
