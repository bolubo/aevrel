#!/usr/bin/env python3
"""Write one target-ready rank report for a validation Job."""

from __future__ import annotations

import argparse
import json
import os
import sys

from aevrel.verification.target_report import (
    TargetReportError,
    environment_rank,
    resolve_report_path,
    write_target_ready_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Write the per-rank target-ready JSON consumed by the "
            "aevrel controller."
        )
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Defaults to AEVREL_REPORT_PATH.",
    )
    parser.add_argument(
        "--migration-uid",
        default=os.environ.get("AEVREL_MIGRATION_UID", ""),
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("AEVREL_VERSION", ""),
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=None,
        help="Defaults to JOB_COMPLETION_INDEX.",
    )
    parser.add_argument("--role", default="worker")
    parser.add_argument("--model-loaded", action="store_true")
    parser.add_argument("--optimizer-loaded", action="store_true")
    parser.add_argument("--steps-ran", type=int, required=True)
    parser.add_argument("--state-complete", action="store_true")
    parser.add_argument("--not-ok", action="store_true")
    parser.add_argument("--error", default="")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rank = environment_rank() if args.rank is None else args.rank
        payload = write_target_ready_report(
            migration_uid=args.migration_uid,
            version=args.version,
            rank=rank,
            role=args.role,
            model_loaded=args.model_loaded,
            optimizer_loaded=args.optimizer_loaded,
            steps_ran=args.steps_ran,
            state_complete=args.state_complete,
            ok=not args.not_ok,
            error=args.error,
            path=args.report_path,
        )
        destination = resolve_report_path(args.report_path)
    except (TargetReportError, OSError, ValueError) as exc:
        print(f"target-report: {exc}", file=sys.stderr)
        return 2
    if not args.quiet:
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(f"wrote {destination}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
