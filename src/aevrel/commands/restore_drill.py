"""CLI wrapper for a real target-command restore drill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..local.drill import run_restore_drill
from ..local.errors import LocalError
from ..theme import print_section, print_result_block


def _kv(label: str, value: object, width: int = 12) -> None:
    print(f"  {label:<{width}} {value}")


def run_restore_drill_command(args) -> int:
    report = (
        Path(args.report).expanduser().resolve()
        if args.report
        else (Path.cwd() / "aevrel-drill-report.json")
    )
    try:
        result = run_restore_drill(
            args.checkpoint,
            target_message=args.target_command,
            target_world_size=args.target_world,
            rank=args.rank,
            steps=args.steps,
            version=args.version,
            migration_uid=args.migration_uid,
            report_path=report,
            timeout=args.timeout,
            require_state_complete=args.require_state_complete,
        )
    except LocalError as exc:
        if args.json:
            print(
                json.dumps(
                    {"status": "failed", "error": exc.to_dict()},
                    indent=2,
                )
            )
        else:
            print(
                f"aevrel restore-drill: {exc.code}: {exc}",
                file=sys.stderr,
            )
            print(f"RESULT: FAILED ({exc.code})", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_section("RESTORE DRILL")
        _kv("checkpoint", result["checkpoint"])
        _kv("rank", result["rank"])
        _kv("steps", result["steps"])
        _kv("report", result["report"])
        _kv("duration", f"{result['duration_seconds']:.3f}s")
        print_result_block(True)
    return 0
