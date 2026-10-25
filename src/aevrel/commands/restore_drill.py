"""CLI wrapper for a real target-command restore drill."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ..local.drill import run_restore_drill
from ..local.errors import LocalError


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
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("restore drill complete")
        print(f"  checkpoint: {result['checkpoint']}")
        print(f"  rank:       {result['rank']}")
        print(f"  steps:      {result['steps']}")
        print(f"  report:     {result['report']}")
        print(f"  duration:   {result['duration_seconds']:.3f}s")
    return 0
