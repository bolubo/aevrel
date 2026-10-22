"""Run the on-demand LRS worker service."""

from __future__ import annotations

import argparse
import os
import signal
import sys

from ..lrs.worker import LiveWorkerServer


def _listen(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        host = "0.0.0.0"
        port_text = value.lstrip(":")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid listen port: {value}") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(f"invalid listen port: {value}")
    return host, port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the on-demand LRS live-rank-split worker",
    )
    parser.add_argument(
        "--listen",
        type=_listen,
        default=_listen("0.0.0.0:9460"),
        metavar="HOST:PORT",
        help="HTTP listen address (default: 0.0.0.0:9460)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SHARDFERRY_LIVE_TOKEN", ""),
        help="bearer token expected from the Go controller",
    )
    parser.add_argument(
        "--state-dir",
        default="/var/lib/shardferry/live-worker",
        help="durable worker/session/proof directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host, port = args.listen
    server = LiveWorkerServer(
        host=host,
        port=port,
        token=args.token,
        state_dir=args.state_dir,
    )

    def _stop(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(
        f"shardferry-live-worker listening on {host}:{port} "
        f"backend=cpu-contract state={args.state_dir}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
