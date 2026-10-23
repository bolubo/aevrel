from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.remote import fetch_spec
from shardferry.verification import verify


def main() -> None:
    parser = argparse.ArgumentParser(
        description="B-side A/B runner: fetch spec, plan, apply, verify"
    )
    parser.add_argument("--server", required=True)
    parser.add_argument("--target-storage", required=True)
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()

    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    client_path = work / "client.yaml"
    plan_path = work / "plan.json"
    report_path = work / "report.json"
    spec = fetch_spec(
        args.server,
        args.target_storage,
        str(client_path),
    )
    plan_payload = plan(spec)
    plan_path.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")
    report = apply(plan_payload, spec)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    verification = verify(spec, plan_payload)
    summary = {
        "server": args.server,
        "work_dir": str(work),
        "client": str(client_path),
        "plan": str(plan_path),
        "report": str(report_path),
        "verification": verification,
    }
    (work / "summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
