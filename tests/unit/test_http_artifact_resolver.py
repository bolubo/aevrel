from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aevrel.agent.executor import ArtifactError, HttpArtifactResolver


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _server(payload: dict, expected_token: str = "secret"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {expected_token}":
                self.send_response(401)
                self.end_headers()
                return
            data = (json.dumps(payload) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_http_resolver_fetches_spec() -> None:
    server, thread = _server({"version": "1", "nodes": []})
    try:
        resolver = HttpArtifactResolver(
            f"http://{_local_ip()}:{server.server_address[1]}",
            "secret",
        )
        assert resolver.resolve("sha256:" + "a" * 64)["version"] == "1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_resolver_rejects_bad_token() -> None:
    server, thread = _server({"version": "1"}, expected_token="other")
    try:
        resolver = HttpArtifactResolver(
            f"http://{_local_ip()}:{server.server_address[1]}",
            "secret",
        )
        with pytest.raises(ArtifactError):
            resolver.resolve("sha256:" + "a" * 64)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
