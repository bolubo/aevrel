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

    codecs = {codec.name: codec}
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
    for chunk in chunks:
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
        "network": {
            "gbps": args.network_gbps,
            "raw_transfer_seconds": raw_transfer_seconds,
            "encoded_transfer_seconds": encoded_transfer_seconds,
            "projected_seconds": projected_seconds,
            "net_gain_seconds": raw_transfer_seconds - projected_seconds,
        },
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
