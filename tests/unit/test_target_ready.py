from __future__ import annotations

import sys
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shardferry.verification import (
    CommandTargetReady,
    HttpTargetReady,
    TargetReadyChain,
    TargetReadyError,
)


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def test_command_target_ready_substitutes_version() -> None:
    command = CommandTargetReady(
        [
            sys.executable,
            "-c",
            "import sys; raise SystemExit(0 if sys.argv[1] == 'step-100' else 1)",
            "{version_id}",
        ]
    )
    command("step-100")


def test_command_target_ready_fails_closed() -> None:
    command = CommandTargetReady(
        [
            sys.executable,
            "-c",
            "import sys; print('not ready', file=sys.stderr); raise SystemExit(7)",
        ]
    )
    with pytest.raises(TargetReadyError, match="exited 7"):
        command("step-100")


def test_http_target_ready_retries_until_ready() -> None:
    calls = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls["count"] += 1
            if calls["count"] < 3:
                self.send_response(503)
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ready = HttpTargetReady(
            f"http://{_local_ip()}:{server.server_address[1]}/ready?version="
            "{version_id}",
            timeout=2,
            interval=0.01,
        )
        ready("step-100")
        assert calls["count"] == 3
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_target_ready_fails_closed_on_timeout() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ready = HttpTargetReady(
            f"http://{_local_ip()}:{server.server_address[1]}/ready",
            timeout=0.15,
            interval=0.01,
        )
        with pytest.raises(TargetReadyError, match="timed out"):
            ready("step-100")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_target_ready_chain_runs_in_order() -> None:
    events: list[tuple[str, str]] = []

    def first(version_id: str) -> None:
        events.append((version_id, "probe"))

    def second(version_id: str) -> None:
        events.append((version_id, "job"))

    TargetReadyChain(first, second)("step-100")
    assert events == [
        ("step-100", "probe"),
        ("step-100", "job"),
    ]
