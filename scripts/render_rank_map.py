from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", default="aevrel-source")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--world-size", type=int, required=True)
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rank_map = [
        {
            "rank": rank,
            "endpoint": (
                f"http://{args.service}-{rank}.{args.service}."
                f"{args.namespace}.svc.cluster.local:{args.port}"
            ),
        }
        for rank in range(args.world_size)
    ]
    Path(args.out).write_text(
        yaml.safe_dump(rank_map, sort_keys=False),
        encoding="utf-8",
    )
    print(yaml.safe_dump(rank_map, sort_keys=False), end="")


if __name__ == "__main__":
    main()
