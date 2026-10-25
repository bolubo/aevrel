#!/usr/bin/env python3
"""Benchmark bitwise verification against tools anyone can run.

Aevrel's verification answers a question `cmp` cannot: after a 4 -> 3 reshard the
target file is not any single source file, so the expected bytes have to be
reduced from several sources first. That makes `cmp` a lower bound rather than a
rival, and `sha256sum` the conventional alternative. Both are measured on the
same machine, over the same payload, in the same run.

Usage:

    python experiments/verify_benchmark.py --size-mb 256
    python experiments/verify_benchmark.py --size-mb 64 --json

Every command prints throughput in MiB/s so the numbers can be checked against
`cmp` and `sha256sum` output directly. The two bounds are measured on the largest
file the migration produced, process start-up included, which is generous to the
bounds: a colder or smaller payload only makes them look better, never worse.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aevrel.dcp import export_dcp, migrate_dcp
from aevrel.executor import apply
from aevrel.planning import plan as build_plan
from aevrel.verification.verify import verify
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


def best_of(function, repeats: int):
    """Fastest of `repeats`, because a cold first run measures the page cache."""
    best = float("inf")
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = function()
        best = min(best, time.perf_counter() - start)
    return best, result


def build_source(root: Path, size_mb: int, source_world: int) -> Path:
    elements = max(1, size_mb * 1024 * 1024 // 4)
    spec, arrays, _ = generate_sharded_spec(
        (elements,), (source_world,), ("Shard(0)",), (source_world,), ("Shard(0)",)
    )
    materialize_source_files(spec, arrays, root)
    apply(build_plan(spec), spec)
    checkpoint = root / "source.dcp"
    export_dcp(spec, checkpoint)
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--source-world", type=int, default=4)
    parser.add_argument("--target-world", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="aevrel-verify-bench-"))
    try:
        checkpoint = build_source(root, args.size_mb, args.source_world)
        migration = migrate_dcp(
            checkpoint,
            output=root / "target.dcp",
            work_dir=root / "work",
            target_world_size=args.target_world,
            keep_work=True,
        )
        spec = migration["spec"]
        plan = migration["plan"]
        total_bytes = int(plan["total_bytes"])

        seconds, _ = best_of(
            lambda: verify(spec, plan, target_root=root / "work" / "target"),
            args.repeats,
        )
        verify_rate = total_bytes / 1024 / 1024 / seconds

        payload_files = sorted(
            (path for path in (root / "target.dcp").rglob("*") if path.is_file()),
            key=lambda path: path.stat().st_size,
            reverse=True,
        )
        left = root / "bound-a.bin"
        right = root / "bound-b.bin"
        shutil.copyfile(payload_files[0], left)
        shutil.copyfile(left, right)
        bound_mb = left.stat().st_size / 1024 / 1024

        cmp_seconds, _ = best_of(
            lambda: subprocess.run(
                ["cmp", "-s", str(left), str(right)], check=False
            ),
            args.repeats,
        )

        def sha_pair():
            for path in (left, right):
                subprocess.run(["sha256sum", str(path)], check=True, capture_output=True)

        sha_seconds, _ = best_of(sha_pair, args.repeats)

        cmp_rate = bound_mb / cmp_seconds
        sha_rate = bound_mb / sha_seconds
        report = {
            "status": "ok",
            "size_mb": args.size_mb,
            "world": [args.source_world, args.target_world],
            "nodes": len(plan["nodes"]),
            "bytes_verified": total_bytes,
            "bound_file_mib": round(bound_mb, 1),
            "aevrel_verify": {
                "seconds": round(seconds, 6),
                "mib_per_second": round(verify_rate, 1),
            },
            "cmp_lower_bound": {
                "seconds": round(cmp_seconds, 6),
                "mib_per_second": round(cmp_rate, 1),
            },
            "sha256sum_both_files": {
                "seconds": round(sha_seconds, 6),
                "mib_per_second": round(sha_rate, 1),
            },
            "speedup_vs_sha256sum": round(verify_rate / sha_rate, 2),
            "fraction_of_cmp_bound": round(verify_rate / cmp_rate, 4),
        }

        if args.json_output:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"payload {total_bytes / 1024 / 1024:.0f} MiB, world "
                  f"{args.source_world} -> {args.target_world}, "
                  f"{report['nodes']} nodes, "
                  f"bound file {bound_mb:.0f} MiB\n")
            print(f"{'implementation':38} {'seconds':>8} {'MiB/s':>9}")
            print(f"{'Aevrel verify (bitwise, run-aware)':38} {seconds:8.3f} "
                  f"{verify_rate:9.0f}")
            print(f"{'cmp -s (physical lower bound)':38} {cmp_seconds:8.3f} "
                  f"{cmp_rate:9.0f}")
            print(f"{'sha256sum both files (conventional)':38} {sha_seconds:8.3f} "
                  f"{sha_rate:9.0f}")
            print(f"\n{report['speedup_vs_sha256sum']}x sha256sum, "
                  f"{report['fraction_of_cmp_bound']:.0%} of the cmp bound")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
