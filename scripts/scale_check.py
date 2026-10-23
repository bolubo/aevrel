from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path

import yaml

from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.verification import verify


GIB = 1024**3
COLS = 1024
ROW_BYTES = COLS * 4


def zero_checksum(size: int) -> str:
    digest = hashlib.sha256()
    block = b"\0" * (1024 * 1024)
    remaining = size
    while remaining:
        take = min(remaining, len(block))
        digest.update(block[:take])
        remaining -= take
    return "sha256:" + digest.hexdigest()


def make_sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gib", type=float, default=1.0)
    parser.add_argument("--source-world", type=int, default=8)
    parser.add_argument("--target-world", type=int, default=5)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    root = Path(args.work_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"work directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    rows = int(args.gib * GIB / ROW_BYTES)
    if rows % args.source_world:
        raise SystemExit("row count must divide the source world size")
    rows_per_rank = rows // args.source_world
    source_dir = root / "source"
    target_dir = root / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    shards = []
    for rank in range(args.source_world):
        path = source_dir / f"rank{rank}.bin"
        make_sparse(path, rows_per_rank * ROW_BYTES)
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": path.as_uri(),
                "offset": [rank * rows_per_rank, 0],
                "size": [rows_per_rank, COLS],
                "checksum": zero_checksum(rows_per_rank * ROW_BYTES),
            }
        )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [rows, COLS], "dtype": "float32"}],
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
    print(f"plan bytes={plan_payload['total_bytes']} nodes={len(plan_payload['nodes'])}")
    if not args.plan_only:
        report = apply(plan_payload, spec)
        verify(spec, plan_payload)
        print(f"apply report={report}")
    print(f"elapsed_seconds={time.perf_counter() - started:.3f}")


if __name__ == "__main__":
    main()
