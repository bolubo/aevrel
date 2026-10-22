from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from shardferry.dcp import export_dcp


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
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    size = args.size_mb * 1024 * 1024
    elements = size // 4
    root = Path(tempfile.mkdtemp(prefix="shardferry-dcp-export-"))
    target = root / "target"
    shard = target / "w" / "rank0.bin"
    shard.parent.mkdir(parents=True)
    shard.touch()
    with shard.open("r+b") as stream:
        stream.truncate(elements * 4)

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [elements], "dtype": "float32"}],
        },
        "source": {
            "mesh": {"shape": [1], "world_size": 1, "placements": ["Shard(0)"]},
            "storage": {"uri": "file:///source"},
            "shards": [{
                "tensor": "w",
                "rank": 0,
                "uri": "file:///source/w/rank0.bin",
                "offset": [0],
                "size": [elements],
                "checksum": "sha256:" + "0" * 64,
            }],
        },
        "target": {
            "mesh": {"shape": [1], "world_size": 1, "placements": ["Shard(0)"]},
            "storage": {"uri": target.as_uri()},
        },
    }

    tracemalloc.start()
    before = rss_kb()
    started = time.perf_counter()
    output = root / "checkpoint.dcp"
    export_dcp(spec, output)
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    distcp = next(output.glob("*.distcp"))
    print(json.dumps({
        "size_mb": args.size_mb,
        "seconds": seconds,
        "python_peak_bytes": peak,
        "rss_before_kb": before,
        "rss_after_kb": rss_kb(),
        "distcp_bytes": distcp.stat().st_size,
    }, sort_keys=True))
    if not args.keep:
        for path in sorted(
            root.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()


if __name__ == "__main__":
    main()
