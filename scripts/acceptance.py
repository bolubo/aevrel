from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jsonschema import Draft202012Validator

from shardferry.demo import run_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True)
    args = parser.parse_args()
    work = Path(args.work_dir).resolve()
    result = run_demo(work)
    for schema in sorted((ROOT / "spec").glob("*.schema.json")):
        Draft202012Validator.check_schema(
            json.loads(schema.read_text(encoding="utf-8"))
        )
    result["schemas_checked"] = True
    (work / "acceptance.json").write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
