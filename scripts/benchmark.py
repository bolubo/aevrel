from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import yaml

from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.verification import verify


MiB = 1024 * 1024


def zero_checksum(size: int) -> str:
    digest = hashlib.sha256()
    block = b"\0" * MiB
    while size:
        take = min(size, len(block))
        digest.update(block[:take])
        size -= take
    return "sha256:" + digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=16)
    parser.add_argument("--source-world", type=int, default=8)
    parser.add_argument("--target-world", type=int, default=5)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args()

    root = Path(args.work_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"work directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    source_dir = root / "source"
    target_dir = root / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    rows = args.source_world
    row_elements = max(1, args.size_mb * MiB // (rows * 4))
    total_bytes = rows * row_elements * 4
    shards = []
    for rank in range(rows):
        path = source_dir / f"rank{rank}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(row_elements * 4)
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": path.as_uri(),
                "offset": [rank, 0],
                "size": [1, row_elements],
                "checksum": zero_checksum(row_elements * 4),
            }
        )

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {
                    "name": "w",
                    "shape": [rows, row_elements],
                    "dtype": "float32",
                }
            ],
        },
        "source": {
            "mesh": {
                "shape": [args.source_world],
                "world_size": args.source_world,
                "placements": ["Shard(0)"],
            },
            "storage": {"uri": source_dir.as_uri()},
            "shards": shards,
        },
        "target": {
            "mesh": {
                "shape": [args.target_world],
                "world_size": args.target_world,
                "placements": ["Shard(0)"],
            },
            "storage": {"uri": target_dir.as_uri()},
        },
    }
    spec_path = root / "migration.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    started = time.perf_counter()
    plan_payload = plan(spec)
    plan_elapsed = time.perf_counter() - started
    apply_started = time.perf_counter()
    report = apply(plan_payload, spec)
    apply_elapsed = time.perf_counter() - apply_started
    verify_elapsed = None
    if not args.skip_verify:
        verify_started = time.perf_counter()
        verify(spec, plan_payload)
        verify_elapsed = time.perf_counter() - verify_started

    source_bytes = total_bytes
    target_bytes = sum(
        region_size
        for region_size in [
            max(0, (rows // args.target_world + (1 if rank < rows % args.target_world else 0)))
            * row_elements
            * 4
            for rank in range(args.target_world)
        ]
    )
    baseline_bytes = source_bytes + target_bytes
    ratio = baseline_bytes / plan_payload["total_bytes"] if plan_payload["total_bytes"] else None
    result = {
        "source_bytes": source_bytes,
        "target_bytes": target_bytes,
        "aevrel_plan_bytes": plan_payload["total_bytes"],
        "baseline_transfer_bytes": baseline_bytes,
        "saving_ratio": ratio,
        "nodes": len(plan_payload["nodes"]),
        "plan_seconds": plan_elapsed,
        "apply_seconds": apply_elapsed,
        "verify_seconds": verify_elapsed,
        "report_status": report["status"],
        "report_bytes": report["bytes_moved"],
    }
    (root / "benchmark.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
