from __future__ import annotations

import argparse
import json
from pathlib import Path

from shardferry.coordinator import expected_rank_work
from shardferry.manifest import load_spec
from shardferry.reports import load_reports, summarize_reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--spec")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if bool(args.plan) != bool(args.spec):
        parser.error("--plan and --spec must be provided together")
    expected_by_rank = None
    if args.plan:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        spec = load_spec(args.spec)
        expected_by_rank = expected_rank_work(plan, spec)
    summary = summarize_reports(
        load_reports(args.reports_dir),
        expected_by_rank=expected_by_rank,
    )
    Path(args.out).write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
