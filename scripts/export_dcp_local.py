from __future__ import annotations

import argparse
import json
from pathlib import Path

from aevrel.dcp import export_dcp_local


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--rank", type=int, required=True)
    args = parser.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    export_dcp_local(
        spec,
        args.output,
        raw_root=args.raw_root,
        ranks=(args.rank,),
        force=True,
    )


if __name__ == "__main__":
    main()
