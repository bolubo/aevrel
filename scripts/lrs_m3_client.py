#!/usr/bin/env python3
"""LRS M3 HTTP client: run the W=8 scenario against a receiver."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aevrel.lrs.scenario import run_m3_scenario  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--steps", type=int, default=24)
    parser.add_argument("--begin", type=int, default=4)
    parser.add_argument("--cutover", type=int, default=12)
    parser.add_argument("--in-flight", type=int, default=2)
    parser.add_argument("--max-lag", type=int, default=2)
    parser.add_argument("--step-delay", type=float, default=0.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    result = run_m3_scenario(
        server_url=args.server,
        steps=args.steps,
        begin_step=args.begin,
        cutover_step=args.cutover,
        in_flight=args.in_flight,
        max_lag=args.max_lag,
        step_delay=args.step_delay,
    )
    Path(args.output).write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": result["ok"],
                "checks": result["checks"],
                "lag_history_max": result["lag_history_max"],
                "theta_digest": result["theta_digest_migrated"],
            },
            sort_keys=True,
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
