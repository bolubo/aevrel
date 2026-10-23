from __future__ import annotations

import argparse
import json
import struct
import tempfile
import time
import tracemalloc
import zlib
from pathlib import Path

from shardferry.dcp.range import inspect_zip_tensor


CHUNK = 4 * 1024 * 1024


def build_sparse_stored_zip(path: Path, size: int) -> None:
    name = b"__0_0/data/0"
    crc = 0
    zeros = b"\0" * min(CHUNK, max(1, size))
    remaining = size
    while remaining:
        part = zeros[: min(len(zeros), remaining)]
        crc = zlib.crc32(part, crc)
        remaining -= len(part)

    zip64 = size > 0xFFFFFFFF
    extra = struct.pack("<HHQQ", 0x0001, 16, size, size) if zip64 else b""
    local_size = 0xFFFFFFFF if zip64 else size
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        45 if zip64 else 20,
        0,
        0,
        0,
        0,
        crc & 0xFFFFFFFF,
        local_size,
        local_size,
        len(name),
        len(extra),
    )
    with path.open("wb") as stream:
        stream.write(local)
        stream.write(name)
        stream.write(extra)
        data_offset = stream.tell()
        stream.truncate(data_offset + size)
        central_offset = data_offset + size
        central_size = (
            struct.calcsize("<IHHHHHHIIIHHHHHII")
            + len(name)
            + len(extra)
        )
        zip64_layout = zip64 or central_offset > 0xFFFFFFFF
        if zip64_layout and not zip64:
            extra = struct.pack("<HHQQ", 0x0001, 16, size, size)
        central_extra = extra
        central_size = (
            struct.calcsize("<IHHHHHHIIIHHHHHII")
            + len(name)
            + len(central_extra)
        )
        central = struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            45 if zip64_layout else 20,
            45 if zip64_layout else 20,
            0,
            0,
            0,
            0,
            crc & 0xFFFFFFFF,
            0xFFFFFFFF if zip64_layout else size,
            0xFFFFFFFF if zip64_layout else size,
            len(name),
            len(central_extra),
            0,
            0,
            0,
            0,
            0,
        )
        stream.seek(central_offset)
        stream.write(central)
        stream.write(name)
        stream.write(central_extra)
        if zip64_layout:
            zip64_offset = central_offset + central_size
            stream.write(
                struct.pack(
                    "<IQHHIIQQQQ",
                    0x06064B50,
                    44,
                    45,
                    45,
                    0,
                    0,
                    1,
                    1,
                    central_size,
                    central_offset,
                )
            )
            stream.write(
                struct.pack(
                    "<IIQI",
                    0x07064B50,
                    0,
                    zip64_offset,
                    1,
                )
            )
            stream.write(
                struct.pack(
                    "<IHHHHIIH",
                    0x06054B50,
                    0,
                    0,
                    0xFFFF,
                    0xFFFF,
                    0xFFFFFFFF,
                    0xFFFFFFFF,
                    0,
                )
            )
        else:
            stream.write(
                struct.pack(
                    "<IHHHHIIH",
                    0x06054B50,
                    0,
                    0,
                    1,
                    1,
                    central_size,
                    central_offset,
                    0,
                )
            )


def rss_kb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--range-mb", type=int, default=1)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    size = args.size_mb * 1024 * 1024
    range_size = min(args.range_mb * 1024 * 1024, size)
    work = Path(tempfile.mkdtemp(prefix="shardferry-dcp-range-"))
    archive = work / "benchmark.distcp"
    build_sparse_stored_zip(archive, size)
    try:
        tracemalloc.start()
        started = time.perf_counter()
        tensor_range = inspect_zip_tensor(archive)
        inspect_seconds = time.perf_counter() - started

        started = time.perf_counter()
        checksum = tensor_range.checksum(archive)
        checksum_seconds = time.perf_counter() - started

        started = time.perf_counter()
        payload = tensor_range.read(
            archive,
            max(0, (size - range_size) // 2),
            range_size,
        )
        range_seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        result = {
            "size_mb": args.size_mb,
            "range_mb": args.range_mb,
            "archive_bytes": archive.stat().st_size,
            "range_bytes": len(payload),
            "checksum": checksum,
            "inspect_seconds": inspect_seconds,
            "checksum_seconds": checksum_seconds,
            "range_seconds": range_seconds,
            "python_peak_bytes": peak,
            "rss_kb": rss_kb(),
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        if not args.keep:
            archive.unlink(missing_ok=True)
            work.rmdir()


if __name__ == "__main__":
    main()
