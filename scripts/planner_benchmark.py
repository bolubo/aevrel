from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from shardferry.planning import plan, validate_plan

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.fixtures.generated import generate_sharded_spec


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-world", type=int, default=1024)
    parser.add_argument("--target-world", type=int, default=512)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be positive")

    spec, _, _ = generate_sharded_spec(
        (max(args.source_world, args.target_world), 4),
        (args.source_world,),
        ("Shard(0)",),
        (args.target_world,),
        ("Shard(0)",),
    )
    timings = []
    result = None
    for _ in range(args.repeat):
        started = time.perf_counter()
        result = plan(spec)
        timings.append(time.perf_counter() - started)
    assert result is not None
    validate_plan(result, spec)
    print(
        json.dumps(
            {
                "source_world": args.source_world,
                "target_world": args.target_world,
                "nodes": len(result["nodes"]),
                "bytes": result["total_bytes"],
                "range_reads": result["cost"]["range_reads"],
                "seconds": timings,
                "best_seconds": min(timings),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
