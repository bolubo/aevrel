from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .cdp import (
    CDPError,
    CheckpointFollower,
    ChunkingPolicy,
    FenceStore,
    VersionTarget,
    scan_versions,
)
from .cutover import (
    CutoverError,
    CutoverPolicy,
    CutoverSession,
)
from .follower import FollowerLoop
from .tls import server_context


class CutoverServiceError(RuntimeError):
    """Raised when a cutover service request is invalid."""


class CutoverService:
    """Small control-plane facade over the durable cutover primitives."""

    def __init__(
        self,
        source_root: str | Path,
        target_root: str | Path,
        *,
        owner: str = "shardferry-controller",
        epoch: int = 1,
        chunking: ChunkingPolicy | None = None,
        policy: CutoverPolicy | None = None,
        target_ready: Callable[[str], None] | None = None,
        follow_interval: float | None = None,
        max_versions_per_cycle: int = 8,
    ) -> None:
        if follow_interval is not None and follow_interval <= 0:
            raise ValueError("follow_interval must be positive")
        self.source_root = Path(source_root).resolve()
        self.target = VersionTarget(target_root)
        self.owner = owner
        self.epoch = epoch
        self.chunking = chunking or ChunkingPolicy.file()
        self.policy = policy or CutoverPolicy()
        self._sync_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self.follower = CheckpointFollower(
            self.source_root,
            self.target,
            chunking=self.chunking,
        )
        self.follow_loop = FollowerLoop(
            self.source_root,
            self.target,
            follower=self.follower,
            lock=self._sync_lock,
            poll_interval=follow_interval or 0.0,
            max_versions_per_cycle=max_versions_per_cycle,
        )
        self.session = CutoverSession(
            self.source_root,
            self.target,
            policy=self.policy,
            chunking=self.chunking,
            follower=self.follower,
            target_ready=target_ready,
        )
        self._follow_thread: threading.Thread | None = None
        self._follow_stop: threading.Event | None = None

    def status(self) -> dict[str, Any]:
        descriptors = scan_versions(self.source_root)
        manifests = self.target.manifests.list()
        positions = {
            descriptor.version_id: index
            for index, descriptor in enumerate(descriptors)
        }
        target_index = max(
            (
                positions.get(manifest.version_id, -1)
                for manifest in manifests
            ),
            default=-1,
        )
        current = self.target.current()
        fence = FenceStore(self.target.root).current()
        lag = (
            len(descriptors) - 1 - target_index
            if target_index >= 0
            else len(descriptors)
        )
        state = "following"
        if fence is not None and (
            current is None or current.version_id != fence.version_id
        ):
            state = "cutover-incomplete"
        elif lag > 0:
            state = "catching-up"
        elif (
            fence is not None
            and current is not None
            and current.version_id == fence.version_id
        ):
            state = "promoted"
        follower = self.follow_loop.status()
        return {
            "state": state,
            "source_latest": (
                descriptors[-1].version_id if descriptors else None
            ),
            "target_latest": (
                descriptors[target_index].version_id
                if target_index >= 0
                else None
            ),
            "lag_versions": lag,
            "current": current.to_dict() if current else None,
            "fence": fence.to_dict() if fence else None,
            "versions": [descriptor.version_id for descriptor in descriptors],
            "follow_state": follower.state,
            "follow_error": follower.last_error,
            "follow_last_success_at": follower.last_success_at,
            "follow_synced_versions": follower.synced_versions,
            "follow_last_missing_bytes": follower.last_missing_bytes,
            "follow_total_missing_bytes": follower.total_missing_bytes,
        }

    def follow_once(self) -> list[dict[str, Any]]:
        results = self.follow_loop.sync_once()
        return [
            {
                "version_id": result.version_id,
                "parent_id": result.parent_id,
                "total_bytes": result.total_bytes,
                "missing_bytes": result.missing_bytes,
                "reused_bytes": result.reused_bytes,
                "staged": result.staged,
                "delta_ratio": result.delta_ratio,
            }
            for result in results
        ]

    def start_following(self) -> None:
        """Start background catch-up, keeping a warm target shadow."""
        if self.follow_loop.poll_interval <= 0:
            raise ValueError("background following is disabled")
        with self._lifecycle_lock:
            if (
                self._follow_thread is not None
                and self._follow_thread.is_alive()
            ):
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self.follow_loop.run,
                args=(stop,),
                name="shardferry-follower",
                daemon=True,
            )
            self._follow_stop = stop
            self._follow_thread = thread
            thread.start()

    def stop_following(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            stop = self._follow_stop
            thread = self._follow_thread
            self._follow_stop = None
            self._follow_thread = None
            if stop is not None:
                stop.set()
        if thread is not None:
            thread.join(timeout=timeout)

    def close(self) -> None:
        self.stop_following()

    def cutover(
        self,
        version_id: str | None = None,
        *,
        owner: str | None = None,
        epoch: int | None = None,
    ) -> dict[str, Any]:
        descriptors = scan_versions(self.source_root)
        selected = version_id or (
            descriptors[-1].version_id if descriptors else None
        )
        if selected is None:
            raise CutoverServiceError("no complete source version is available")
        with self._sync_lock:
            result = self.session.cutover(
                selected,
                owner=owner or self.owner,
                epoch=epoch if epoch is not None else self.epoch,
            )
        return {
            "state": result.state,
            "version_id": result.version_id,
            "previous_version": result.previous_version,
            "manifest_root": result.manifest_root,
            "paused_seconds": result.paused_seconds,
            "target_ready_seconds": result.target_ready_seconds,
            "fence": result.fence.to_dict(),
        }

    def recover(self, *, owner: str | None = None) -> dict[str, Any]:
        with self._sync_lock:
            result = self.session.recover(owner=owner or self.owner)
        return {
            "state": result.state,
            "version_id": result.version_id,
            "previous_version": result.previous_version,
            "resumed": result.resumed,
            "fence": result.fence.to_dict() if result.fence else None,
        }


def build_cutover_server(
    listen: str,
    service: CutoverService,
    token: str,
    *,
    tls_cert: str | None = None,
    tls_key: str | None = None,
    tls_ca: str | None = None,
) -> ThreadingHTTPServer:
    if not token:
        raise CutoverServiceError("cutover service token must not be empty")
    host, port = _parse_listen(listen)
    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(service, token),
    )
    server.daemon_threads = True
    context = server_context(tls_cert, tls_key, tls_ca)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def _make_handler(service: CutoverService, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if urlsplit(self.path).path == "/v1/status":
                self._json(service.status())
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if path == "/v1/follow":
                    self._json({"results": service.follow_once()})
                elif path == "/v1/cutover":
                    self._json(
                        service.cutover(
                            body.get("version_id"),
                            owner=body.get("owner"),
                            epoch=body.get("epoch"),
                        )
                    )
                elif path == "/v1/recover":
                    self._json(service.recover(owner=body.get("owner")))
                else:
                    self.send_error(404)
            except (CDPError, CutoverError, CutoverServiceError, ValueError) as exc:
                self._json({"error": str(exc)}, status=409)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {token}":
                return True
            self._json({"error": "unauthorized"}, status=401)
            return False

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise CutoverServiceError("invalid Content-Length") from exc
            if length > 1024 * 1024:
                raise CutoverServiceError("request body is too large")
            data = self.rfile.read(length)
            if not data:
                return {}
            try:
                body = json.loads(data)
            except ValueError as exc:
                raise CutoverServiceError("request body is not valid JSON") from exc
            if not isinstance(body, dict):
                raise CutoverServiceError("request body must be an object")
            return body

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _parse_listen(listen: str) -> tuple[str, int]:
    parsed = urlsplit("//" + listen)
    if parsed.hostname is None or parsed.port is None:
        raise CutoverServiceError(f"invalid listen address: {listen}")
    return parsed.hostname, parsed.port
