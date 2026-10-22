#!/usr/bin/env python3
"""LRS M3 HTTP delta receiver."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs.transport import HttpDeltaServer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    server = HttpDeltaServer(args.host, args.port, args.state_dir)
    server.start()
    host, port = server.address
    print(f"LRS_M3_SERVER {host}:{port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
