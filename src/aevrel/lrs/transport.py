"""Cross-cluster delta transport for LRS M3."""

from __future__ import annotations

import http.client
import json
import os
import queue
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .protocol import DigestMismatchError, LrsError
from .state import ShardBlob


_CHANNEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class DeltaSink(Protocol):
    """Ordered, resumable delta stream from one source shard to one target."""

    def send(self, blob: ShardBlob) -> None:
        ...

    def wait_for(self, step: int) -> None:
        ...

    def acked_digest(self, step: int) -> str:
        ...

    def acked_blob(self, step: int) -> ShardBlob:
        ...

    def close(self) -> None:
        ...

    @property
    def last_acked_step(self) -> int:
        ...

    @property
    def last_acked_digest(self) -> str | None:
        ...


class HttpDeltaSink:
    """Asynchronous HTTP sink with ordered retry and resume.

    ``send`` never blocks. A single worker sends blobs in order, retries
    transient failures, and asks the receiver for its last ack after a
    reconnect. ``wait_for`` is the only blocking operation, used at cutover
    and for bounded-lag throttling.
    """

    def __init__(
        self,
        base_url: str,
        channel: str,
        *,
        timeout: float = 5.0,
        retry_delay: float = 0.05,
        max_retry_delay: float = 1.0,
        wait_timeout: float | None = 30.0,
    ) -> None:
        if not _CHANNEL_RE.fullmatch(channel):
            raise ValueError(f"invalid channel name: {channel!r}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retry_delay <= 0:
            raise ValueError("retry_delay must be positive")
        self.base_url = base_url.rstrip("/")
        self.channel = channel
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self.wait_timeout = wait_timeout
        self._opener = build_opener(ProxyHandler({}))
        self._queue: queue.Queue[ShardBlob | None] = queue.Queue()
        self._condition = threading.Condition()
        self._acked: dict[int, str] = {}
        self._sent: dict[int, ShardBlob] = {}
        self._last_acked = -1
        self._last_acked_digest: str | None = None
        self._closed = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def last_acked_step(self) -> int:
        with self._condition:
            return self._last_acked

    @property
    def last_acked_digest(self) -> str | None:
        with self._condition:
            return self._last_acked_digest

    def send(self, blob: ShardBlob) -> None:
        with self._condition:
            if self._closed:
                raise LrsError(f"transport {self.channel} is closed")
            self._sent[blob.step] = blob
        self._queue.put(blob)

    def wait_for(self, step: int) -> None:
        if step < 0:
            return
        deadline = (
            time.monotonic() + self.wait_timeout
            if self.wait_timeout is not None
            else None
        )
        with self._condition:
            while self._last_acked < step:
                if self._error is not None:
                    raise LrsError(
                        f"transport {self.channel} failed: {self._error}"
                    ) from self._error
                if self._closed:
                    raise LrsError(
                        f"transport {self.channel} closed before step {step}"
                    )
                if deadline is None:
                    self._condition.wait(timeout=0.5)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LrsError(
                        f"transport {self.channel} timed out waiting "
                        f"for step {step}"
                    )
                self._condition.wait(timeout=min(0.5, remaining))

    def acked_digest(self, step: int) -> str:
        with self._condition:
            digest = self._acked.get(step)
            if digest is not None:
                return digest
            if step == self._last_acked and self._last_acked_digest is not None:
                return self._last_acked_digest
            raise LrsError(
                f"transport {self.channel} has no ack for step {step}"
            )

    def acked_blob(self, step: int) -> ShardBlob:
        with self._condition:
            blob = self._sent.get(step)
            if blob is None or self._last_acked < step:
                raise LrsError(
                    f"transport {self.channel} has not acked step {step}"
                )
            return blob

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self._queue.put(None)
        self._thread.join(timeout=self.wait_timeout or self.timeout)

    def _run(self) -> None:
        try:
            while True:
                blob = self._queue.get()
                if blob is None:
                    return
                self._send_with_retry(blob)
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._condition.notify_all()

    def _send_with_retry(self, blob: ShardBlob) -> None:
        delay = self.retry_delay
        while True:
            with self._condition:
                if self._closed:
                    return
                if self._last_acked >= blob.step:
                    return
            try:
                self._post(blob)
                return
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500:
                    raise LrsError(
                        f"server rejected {self.channel} step {blob.step}: "
                        f"{exc.code} {exc.reason}"
                    ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ):
                pass
            try:
                acked_step, acked_digest = self._get_ack()
                with self._condition:
                    self._update_ack(acked_step, acked_digest)
                if acked_step >= blob.step:
                    return
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ):
                pass
            time.sleep(delay)
            delay = min(delay * 2, self.max_retry_delay)

    def _post(self, blob: ShardBlob) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/v1/channels/{self.channel}/blobs",
            data=blob.to_bytes(),
            method="POST",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Shard-Id": str(blob.shard_id),
                "X-Step": str(blob.step),
                "X-Epoch": str(blob.epoch),
                "X-Digest": blob.digest,
            },
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        step = int(payload["step"])
        digest = str(payload["digest"])
        if step == blob.step and digest != blob.digest:
            raise DigestMismatchError(
                f"receiver ack digest mismatch for {self.channel} "
                f"step {blob.step}"
            )
        with self._condition:
            self._update_ack(step, digest)
            self._condition.notify_all()

    def _get_ack(self) -> tuple[int, str]:
        with self._opener.open(
            f"{self.base_url}/v1/channels/{self.channel}/ack",
            timeout=self.timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return int(payload["step"]), str(payload["digest"])

    def _update_ack(self, step: int, digest: str) -> None:
        if step < 0:
            return
        self._acked[step] = digest
        if step > self._last_acked:
            self._last_acked = step
            self._last_acked_digest = digest
        self._condition.notify_all()


class HttpDeltaServer:
    """Threaded HTTP receiver that persists the latest shard blob per channel."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        state_dir: str | Path = ".lrs-state",
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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
        state_dir = self.state_dir
        lock = self._lock

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    self._json({"ok": True})
                    return
                route = _parse_channel_route(parsed.path)
                if route is None or route[1] != "ack":
                    self.send_error(404)
                    return
                channel = route[0]
                with lock:
                    latest = _read_latest(_channel_dir(state_dir, channel))
                self._json(latest)

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                route = _parse_channel_route(parsed.path)
                if route is None or route[1] != "blobs":
                    self.send_error(404)
                    return
                channel = route[0]
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400, "invalid Content-Length")
                    return
                body = self.rfile.read(length)
                try:
                    blob = _parse_blob(body)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                header_digest = self.headers.get("X-Digest")
                if header_digest and header_digest != blob.digest:
                    self.send_error(400, "digest mismatch")
                    return
                with lock:
                    channel_dir = _channel_dir(state_dir, channel)
                    channel_dir.mkdir(parents=True, exist_ok=True)
                    latest = _read_latest(channel_dir)
                    if blob.step <= int(latest["step"]):
                        existing = _read_blob_digest(
                            channel_dir,
                            blob.step,
                        )
                        if existing is not None and existing != blob.digest:
                            self.send_error(409, "step digest conflict")
                            return
                        if existing is None:
                            self._json(latest)
                            return
                        self._json(
                            {"step": blob.step, "digest": blob.digest}
                        )
                        return
                    _write_blob(channel_dir, blob, body)
                    _write_latest(channel_dir, blob.step, blob.digest)
                self._json({"step": blob.step, "digest": blob.digest})

            def _json(self, payload: dict[str, object]) -> None:
                data = (json.dumps(payload) + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _parse_channel_route(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    if len(parts) != 5:
        return None
    if parts[0] != "" or parts[1] != "v1" or parts[2] != "channels":
        return None
    channel, action = parts[3], parts[4]
    if not _CHANNEL_RE.fullmatch(channel):
        return None
    if action not in {"blobs", "ack"}:
        return None
    return channel, action


def _channel_dir(state_dir: Path, channel: str) -> Path:
    if not _CHANNEL_RE.fullmatch(channel):
        raise ValueError(f"invalid channel: {channel!r}")
    return state_dir / channel


def _parse_blob(body: bytes) -> ShardBlob:
    if len(body) < 28:
        raise ValueError("invalid blob length")
    step, shard_id, epoch = struct.unpack("<QQQ", body[:24])
    aux_length = struct.unpack("<I", body[24:28])[0]
    payload = body[28:]
    if aux_length > len(payload):
        raise ValueError("invalid aux length")
    values_length = len(payload) - aux_length
    if values_length % 8 != 0:
        raise ValueError("invalid blob value length")
    count = values_length // 8
    if count % 2 != 0:
        raise ValueError("blob must contain theta and momentum")
    values = struct.unpack(f"<{count}d", payload[:values_length])
    half = count // 2
    return ShardBlob(
        step=step,
        shard_id=shard_id,
        epoch=epoch,
        theta=values[:half],
        momentum=values[half:],
        aux=payload[values_length:],
    )


def _read_latest(channel_dir: Path) -> dict[str, object]:
    path = channel_dir / "latest.json"
    if not path.is_file():
        return {"step": -1, "digest": ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "step": int(payload["step"]),
            "digest": str(payload["digest"]),
        }
    except (OSError, KeyError, TypeError, ValueError):
        return {"step": -1, "digest": ""}


def _write_latest(channel_dir: Path, step: int, digest: str) -> None:
    payload = json.dumps({"step": step, "digest": digest}) + "\n"
    _atomic_write(channel_dir / "latest.json", payload.encode("utf-8"))


def _write_blob(channel_dir: Path, blob: ShardBlob, body: bytes) -> None:
    _atomic_write(channel_dir / f"{blob.step:020d}.blob", body)


def _read_blob_digest(channel_dir: Path, step: int) -> str | None:
    path = channel_dir / f"{step:020d}.blob"
    if not path.is_file():
        return None
    try:
        return _parse_blob(path.read_bytes()).digest
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
