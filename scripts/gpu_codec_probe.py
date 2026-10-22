#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.gpu_codec import (  # noqa: E402
    CodecError,
    NvcompCodec,
    decode_frame,
    encode_frame,
    make_codec,
    nvcomp_status,
)
from shardferry.gpu_adaptive import AdaptiveRangeCodec  # noqa: E402
from shardferry.gpu_batch import AdaptiveBatchCodec  # noqa: E402
from shardferry.gpu_policy import decide_codec  # noqa: E402


def _pattern_data(pattern: str, size: int, seed: int) -> bytes:
    if size < 1:
        raise ValueError("size must be positive")
    if pattern == "zero":
        return bytes(size)
    if pattern == "repeat":
        seed_bytes = b"shardferry-checkpoint-range"
        return (seed_bytes * (size // len(seed_bytes) + 1))[:size]
    if pattern == "random":
        return random.Random(seed).randbytes(size)
    if pattern == "sparse":
        data = bytearray(size)
        for offset in range(0, size, 64 * 1024):
            data[offset : offset + 8] = b"SPARSE!!"
        return bytes(data)
    raise ValueError(f"unsupported pattern: {pattern}")


def _file_chunks(path: Path, range_bytes: int) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(range_bytes)
            if not chunk:
                return
            yield chunk


def _input_chunks(args) -> Iterator[bytes]:
    if args.input is None:
        data = _pattern_data(args.pattern, args.size, args.seed)
        for offset in range(0, len(data), args.range_bytes):
            yield data[offset : offset + args.range_bytes]
        return
    input_path = Path(args.input).expanduser().resolve()
    if input_path.is_file():
        yield from _file_chunks(input_path, args.range_bytes)
        return
    if not input_path.is_dir():
        raise ValueError(f"input does not exist: {input_path}")
    for path in sorted(item for item in input_path.rglob("*") if item.is_file()):
        yield from _file_chunks(path, args.range_bytes)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure framed codec ratio and round-trip latency.",
    )
    parser.add_argument("--input", default=None)
    parser.add_argument(
        "--pattern",
        choices=("zero", "repeat", "random", "sparse"),
        default="zero",
    )
    parser.add_argument("--size", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--range-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-ranges", type=int, default=8)
    parser.add_argument(
        "--codec",
        choices=(
            "identity",
            "zlib",
            "nvcomp-zstd",
            "nvcomp-lz4",
            "nvcomp-gdeflate",
        ),
        default="zlib",
    )
    parser.add_argument("--network-gbps", type=float, default=10.0)
    parser.add_argument("--adaptive", action="store_true")
    parser.add_argument("--batch-ranges", type=int, default=1)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _environment() -> dict:
    result = {"python": sys.version.split()[0]}
    try:
        import torch

        result.update(
            {
                "torch": torch.__version__,
                "cuda": torch.version.cuda,
                "cuda_available": bool(torch.cuda.is_available()),
                "device_count": int(torch.cuda.device_count()),
                "devices": [
                    torch.cuda.get_device_name(index)
                    for index in range(torch.cuda.device_count())
                ],
            }
        )
    except Exception as exc:
        result["torch_error"] = str(exc)
    result["nvcomp"] = nvcomp_status()
    return result


def _run(args) -> dict:
    if args.range_bytes < 1:
        raise ValueError("range-bytes must be positive")
    if args.max_ranges < 1:
        raise ValueError("max-ranges must be positive")
    if args.network_gbps < 0:
        raise ValueError("network-gbps must be non-negative")
    if args.batch_ranges < 1:
        raise ValueError("batch-ranges must be positive")
    if args.batch_ranges > 1 and not args.adaptive:
        raise ValueError("batch-ranges requires --adaptive")
    codec = make_codec(args.codec)
    if isinstance(codec, NvcompCodec) and not codec.available():
        status = nvcomp_status()
        raise CodecError(
            "nvcomp codec is unavailable: "
            f"importable={status['importable']} "
            f"cuda_available={status['cuda_available']} "
            f"error={status['error']}"
        )
    chunks = []
    for chunk in _input_chunks(args):
        chunks.append(chunk)
        if len(chunks) >= args.max_ranges:
            break
    if not chunks:
        raise ValueError("no input ranges found")

    codecs = {codec.name: codec, "identity": make_codec("identity")}
    adaptive_codec = None
    if args.adaptive:
        warm = codec.encode(b"adaptive-warmup" * 1024)
        codec.decode(warm)
        calibration = next(
            (chunk for chunk in chunks if len(chunk) >= 1024 * 1024),
            chunks[0],
        )
        started = time.perf_counter()
        encoded = codec.encode(calibration)
        encode_seconds = time.perf_counter() - started
        started = time.perf_counter()
        decoded = codec.decode(encoded)
        decode_seconds = time.perf_counter() - started
        if decoded != calibration:
            raise CodecError("codec calibration round-trip failed")
        adaptive_codec = AdaptiveRangeCodec(
            codec,
            network_gbps=args.network_gbps,
            min_bytes=min(args.range_bytes, 1024 * 1024),
            compress_mib_s=(
                len(calibration) / (1024**2) / encode_seconds
                if encode_seconds > 0
                else None
            ),
            decompress_mib_s=(
                len(calibration) / (1024**2) / decode_seconds
                if decode_seconds > 0
                else None
            ),
        )
    if hasattr(codec, "synchronize"):
        codec.synchronize()
    warmup = encode_frame(codec, chunks[0])
    decode_frame(warmup, codecs)
    if hasattr(codec, "synchronize"):
        codec.synchronize()

    raw_bytes = 0
    frame_bytes = 0
    compress_seconds = 0.0
    decompress_seconds = 0.0
    roundtrip_ok = True
    adaptive_enabled = 0
    adaptive_skipped = 0
    adaptive_enabled_ranges = 0
    adaptive_skipped_ranges = 0
    adaptive_enabled_batches = 0
    adaptive_skipped_batches = 0
    adaptive_decisions = []
    batch_codec = None
    batches = []
    if adaptive_codec is not None and args.batch_ranges > 1:
        batch_codec = AdaptiveBatchCodec(
            adaptive_codec,
            max_ranges=args.batch_ranges,
            max_bytes=args.batch_ranges * args.range_bytes,
        )
        batches = [
            chunks[index : index + args.batch_ranges]
            for index in range(0, len(chunks), args.batch_ranges)
        ]
    if batch_codec is not None:
        for batch in batches:
            started = time.perf_counter()
            adaptive_result = batch_codec.encode(batch)
            compress_seconds += time.perf_counter() - started
            frame = adaptive_result.frame
            adaptive_decisions.append(adaptive_result.decision.to_dict())
            if adaptive_result.compressed:
                adaptive_enabled += 1
                adaptive_enabled_batches += 1
                adaptive_enabled_ranges += len(batch)
            else:
                adaptive_skipped += 1
                adaptive_skipped_batches += 1
                adaptive_skipped_ranges += len(batch)
            started = time.perf_counter()
            decoded_batch = batch_codec.decode(frame)
            decompress_seconds += time.perf_counter() - started
            raw_bytes += sum(len(chunk) for chunk in batch)
            frame_bytes += len(frame)
            roundtrip_ok = roundtrip_ok and decoded_batch == batch
    else:
        for chunk in chunks:
            if adaptive_codec is not None:
                started = time.perf_counter()
                adaptive_result = adaptive_codec.encode(chunk)
                compress_seconds += time.perf_counter() - started
                frame = adaptive_result.frame
                adaptive_decisions.append(adaptive_result.decision.to_dict())
                if adaptive_result.compressed:
                    adaptive_enabled += 1
                    adaptive_enabled_ranges += 1
                else:
                    adaptive_skipped += 1
                    adaptive_skipped_ranges += 1
            else:
                if hasattr(codec, "synchronize"):
                    codec.synchronize()
                started = time.perf_counter()
                frame = encode_frame(codec, chunk)
                if hasattr(codec, "synchronize"):
                    codec.synchronize()
                compress_seconds += time.perf_counter() - started

            started = time.perf_counter()
            decoded = decode_frame(frame, codecs)
            if hasattr(codec, "synchronize"):
                codec.synchronize()
            decompress_seconds += time.perf_counter() - started
            raw_bytes += len(chunk)
            frame_bytes += len(frame)
            roundtrip_ok = roundtrip_ok and decoded == chunk

    gib = 1024**3
    network_bytes_per_second = args.network_gbps * 1_000_000_000 / 8
    raw_transfer_seconds = (
        raw_bytes / network_bytes_per_second
        if network_bytes_per_second > 0
        else 0.0
    )
    encoded_transfer_seconds = (
        frame_bytes / network_bytes_per_second
        if network_bytes_per_second > 0
        else 0.0
    )
    projected_seconds = (
        encoded_transfer_seconds + compress_seconds + decompress_seconds
    )
    report = {
        "ok": roundtrip_ok,
        "codec": codec.name,
        "input": (
            str(Path(args.input).expanduser().resolve())
            if args.input
            else f"pattern:{args.pattern}"
        ),
        "processed_ranges": len(chunks),
        "range_bytes": args.range_bytes,
        "raw_bytes": raw_bytes,
        "frame_bytes": frame_bytes,
        "compression_ratio": frame_bytes / raw_bytes if raw_bytes else 0.0,
        "compress_seconds": compress_seconds,
        "decompress_seconds": decompress_seconds,
        "compress_mib_s": (
            raw_bytes / (1024**2) / compress_seconds
            if compress_seconds > 0
            else None
        ),
        "decompress_mib_s": (
            raw_bytes / (1024**2) / decompress_seconds
            if decompress_seconds > 0
            else None
        ),
        "roundtrip_ok": roundtrip_ok,
        "adaptive": (
            {
                "enabled_ranges": adaptive_enabled_ranges,
                "skipped_ranges": adaptive_skipped_ranges,
                "batch_ranges": args.batch_ranges,
                "enabled_batches": (
                    adaptive_enabled_batches if batch_codec is not None else 0
                ),
                "skipped_batches": (
                    adaptive_skipped_batches if batch_codec is not None else 0
                ),
                "decisions": adaptive_decisions,
            }
            if adaptive_codec is not None
            else None
        ),
        "network": {
            "gbps": args.network_gbps,
            "raw_transfer_seconds": raw_transfer_seconds,
            "encoded_transfer_seconds": encoded_transfer_seconds,
            "projected_seconds": projected_seconds,
            "net_gain_seconds": raw_transfer_seconds - projected_seconds,
        },
        "recommendation": decide_codec(
            raw_bytes=raw_bytes,
            encoded_bytes=frame_bytes,
            compress_seconds=compress_seconds,
            decompress_seconds=decompress_seconds,
            network_gbps=args.network_gbps,
        ).to_dict(),
        "environment": _environment(),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = _run(args)
    except (CodecError, OSError, ValueError) as exc:
        print(f"gpu-codec-probe: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"codec: {report['codec']}")
        print(f"ranges: {report['processed_ranges']}")
        print(f"ratio: {report['compression_ratio']:.4f}")
        print(
            f"compress: {report['compress_mib_s'] or 0.0:.2f} MiB/s, "
            f"decompress: {report['decompress_mib_s'] or 0.0:.2f} MiB/s"
        )
        print(f"roundtrip: {report['roundtrip_ok']}")
        print(f"net gain at {report['network']['gbps']} Gbps: "
              f"{report['network']['net_gain_seconds']:.3f}s")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
