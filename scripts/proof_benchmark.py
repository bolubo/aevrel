from __future__ import annotations

import argparse
import json
import resource
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path

from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.verification import build_proof, verify_proof

try:
    from scripts.cdp_benchmark import make_version
except ImportError:  # direct `python scripts/proof_benchmark.py`
    from cdp_benchmark import make_version


def rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=1024)
    parser.add_argument("--source-world", type=int, default=128)
    parser.add_argument("--target-world", type=int, default=64)
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    work_parent = Path(args.work_dir).resolve() if args.work_dir else None
    if work_parent is not None:
        work_parent.mkdir(parents=True, exist_ok=True)
    root = Path(
        tempfile.mkdtemp(prefix="sf-proof-bench-", dir=work_parent)
    )
    row_elements = max(
        4,
        args.size_mb * 1024 * 1024 // (args.source_world * 4),
    )
    try:
        spec, _ = make_version(
            root,
            "step-100",
            step=100,
            parent_id=None,
            source_world=args.source_world,
            target_world=args.target_world,
            row_elements=row_elements,
            change_index=None,
            fill="zero",
        )
        plan_payload = plan(spec)
        target_root = root / "target"
        apply(plan_payload, spec, target_root=target_root)

        tracemalloc.start()
        started = time.perf_counter()
        proof = build_proof(
            spec,
            plan_payload,
            target_root,
            source=spec["source"]["storage"]["uri"],
        )
        build_seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]

        started = time.perf_counter()
        target_result = verify_proof(
            proof,
            target_root,
            plan=plan_payload,
        )
        target_verify_seconds = time.perf_counter() - started
        started = time.perf_counter()
        source_result = verify_proof(
            proof,
            target_root,
            plan=plan_payload,
            source=spec["source"]["storage"]["uri"],
        )
        source_verify_seconds = time.perf_counter() - started

        payload = json.dumps(proof, sort_keys=True).encode("utf-8")
        print(json.dumps({
            "size_mb": args.size_mb,
            "source_world": args.source_world,
            "target_world": args.target_world,
            "proof_bytes": len(payload),
            "target_leaves": len(proof["target_leaves"]),
            "source_leaves": len(proof["source_leaves"]),
            "coverage": len(proof["coverage"]),
            "build_seconds": build_seconds,
            "target_verify_seconds": target_verify_seconds,
            "source_verify_seconds": source_verify_seconds,
            "target_only_passed": target_result["passed"],
            "source_reduction_verified": source_result["reduction_verified"],
            "python_peak_bytes": peak,
            "max_rss_mb": rss_kb() / 1024,
        }, indent=2, sort_keys=True))
    finally:
        if not args.keep:
            shutil.rmtree(root)
        else:
            print(f"kept: {root}")


if __name__ == "__main__":
    main()
