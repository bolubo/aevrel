from __future__ import annotations

import argparse
import time
from pathlib import Path

from aevrel.cdp import TargetLock


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--marker", required=True)
    args = parser.parse_args()
    with TargetLock(args.root):
        Path(args.marker).write_text("locked")
        time.sleep(60)


if __name__ == "__main__":
    main()
