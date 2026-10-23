from __future__ import annotations

import argparse
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from shardferry.verification import DcpTargetReady, TargetReadyError


class JobState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.version_id: str | None = None
        self.ready = False
        self.error: str | None = None
        self.load_seconds = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "version_id": self.version_id,
                "ready": self.ready,
                "error": self.error,
                "load_seconds": self.load_seconds,
            }

    def load(self, version_id: str, ready: DcpTargetReady) -> None:
        with self.lock:
            self.version_id = version_id
            self.ready = False
            self.error = None
            started = time.perf_counter()
            try:
                ready(version_id)
                self.ready = True
            except TargetReadyError as exc:
                self.error = str(exc)
            finally:
                self.load_seconds = time.perf_counter() - started


def _watch_control(
    control_file: Path,
    state: JobState,
    ready: DcpTargetReady,
) -> None:
    last = None
    while True:
        try:
            value = control_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value and value != last:
            last = value
            state.load(value, ready)
        time.sleep(0.02)


def _make_handler(state: JobState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path not in {"/readyz", "/status"}:
                self.send_error(404)
                return
            expected = parse_qs(parsed.query).get("version", [None])[0]
            snapshot = state.snapshot()
            ready = snapshot["ready"] and (
                expected is None or snapshot["version_id"] == expected
            )
            payload = dict(snapshot)
            payload["expected_version"] = expected
            data = (json.dumps(payload, sort_keys=True) + "\n").encode()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--metadata", default="")
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--control-file", required=True)
    parser.add_argument("--listen", default="0.0.0.0:0")
    args = parser.parse_args()
    import torch

    # The target job is already resident during warm standby; initialize the
    # backend before the cutover window starts.
    torch.empty(1)
    warmup = io.BytesIO()
    torch.save(torch.empty(1), warmup)
    warmup.seek(0)
    torch.load(warmup, weights_only=False)
    from torch.distributed.checkpoint import FileSystemReader

    import shardferry.dcp  # noqa: F401

    del FileSystemReader
    host, port_text = args.listen.rsplit(":", 1)
    state = JobState()
    server = ThreadingHTTPServer(
        (host, int(port_text)),
        _make_handler(state),
    )
    server.daemon_threads = True
    ready = DcpTargetReady(
        args.checkpoint,
        metadata_dir=args.metadata or None,
        rank=args.rank,
    )
    threading.Thread(
        target=_watch_control,
        args=(Path(args.control_file).resolve(), state, ready),
        daemon=True,
    ).start()
    print(
        f"target-job listening on {host}:{server.server_address[1]}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
