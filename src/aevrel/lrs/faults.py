"""Fault-injection helpers for LRS transport tests and demos."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener


class DroppingProxy:
    """HTTP proxy that drops the first N POST connections.

    This is a dev/test fault injector, not part of the production transport.
    """

    def __init__(
        self,
        upstream: str,
        *,
        drop_posts: int = 0,
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self._drop_posts = int(drop_posts)
        self._dropped = 0
        self._lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}))
        self._server = ThreadingHTTPServer(
            (host, int(port)),
            self._make_handler(),
        )
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                self._forward("GET")

            def do_POST(self) -> None:
                with proxy._lock:
                    if proxy._drop_posts > 0:
                        proxy._drop_posts -= 1
                        proxy._dropped += 1
                        drop = True
                    else:
                        drop = False
                if drop:
                    self.connection.close()
                    return
                self._forward("POST")

            def _forward(self, method: str) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400, "invalid Content-Length")
                    return
                body = self.rfile.read(length) if length else None
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower()
                    not in {"host", "content-length", "connection"}
                }
                request = urllib.request.Request(
                    f"{proxy.upstream}{self.path}",
                    data=body,
                    method=method,
                    headers=headers,
                )
                try:
                    with proxy._opener.open(
                        request,
                        timeout=5.0,
                    ) as response:
                        data = response.read()
                        self.send_response(response.status)
                        self._copy_headers(response.headers)
                        self.send_header(
                            "Content-Length",
                            str(len(data)),
                        )
                        self.end_headers()
                        self.wfile.write(data)
                except urllib.error.HTTPError as exc:
                    data = exc.read()
                    self.send_response(exc.code)
                    self._copy_headers(exc.headers)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (urllib.error.URLError, TimeoutError, OSError):
                    self.send_error(502)

            def _copy_headers(self, headers) -> None:
                for key, value in headers.items():
                    if key.lower() in {
                        "transfer-encoding",
                        "connection",
                        "content-length",
                    }:
                        continue
                    self.send_header(key, value)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler
