from __future__ import annotations

import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import prod
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .agent_state import AgentStateError, TaskPhase, TaskRecord, TaskStore
from .contracts import contract_version, validate_capabilities, validate_error
from .layout import DTYPE_BYTES
from .manifest import ManifestError, load_spec
from .planner import plan as build_plan


class AgentError(RuntimeError):
    """Raised when an agent cannot serve or mutate a task."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AgentApp:
    def __init__(
        self,
        controller_token: str,
        state_dir: str | Path,
        executor: Callable[[TaskRecord], None] | None = None,
        committer: Callable[[TaskRecord], None] | None = None,
        spec_path: str | Path | None = None,
    ) -> None:
        if not controller_token:
            raise AgentError("controller token must not be empty")
        self.controller_token = controller_token
        self.store = TaskStore(state_dir)
        self.executor = executor
        self.committer = committer
        self.spec_path = Path(spec_path).resolve() if spec_path else None
        self.spec = load_spec(self.spec_path) if self.spec_path else None
        self._metadata_hash = (
            _sha256(self.spec_path.read_bytes())
            if self.spec_path is not None
            else None
        )

    def capabilities(self) -> dict[str, Any]:
        payload = {
            "version": contract_version(),
            "apiVersion": "shardferry.io/v1",
            "agentVersion": __version__,
            "roles": ["source", "target"],
            "features": ["dcp", "range-read", "bitwise-verify"],
        }
        validate_capabilities(payload)
        return payload

    def inventory(self, rank: int) -> dict[str, Any]:
        if self.spec is None:
            raise AgentError("inventory is not configured")
        rows = [
            shard
            for shard in self.spec["source"]["shards"]
            if shard["rank"] == rank
        ]
        if not rows:
            raise AgentError(f"source rank not found: {rank}")
        tensors = {
            tensor["name"]: tensor
            for tensor in self.spec["checkpoint"]["tensors"]
        }
        chunks = []
        for shard in rows:
            tensor = tensors[shard["tensor"]]
            chunk_bytes = (
                prod(shard["size"]) * DTYPE_BYTES[tensor["dtype"]]
            )
            chunks.append(
                {
                    "tensor": shard["tensor"],
                    "rank": shard["rank"],
                    "file": shard["uri"],
                    "tensor_offset": shard["offset"],
                    "tensor_size": shard["size"],
                    "byte_ranges": [[0, chunk_bytes]],
                    "checksum": shard["checksum"],
                }
            )
        return {
            "version": contract_version(),
            "rank": rank,
            "format": self.spec["checkpoint"]["format"],
            "metadata_hash": self._metadata_hash,
            "tensors": list(tensors.values()),
            "chunks": chunks,
            "agent_version": __version__,
            "torch_version": "runtime",
        }

    def plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": contract_version(),
            "plan": build_plan(spec),
        }

    def authorize(self, header: str | None) -> bool:
        return header == f"Bearer {self.controller_token}"


def build_agent_server(
    listen: str,
    controller_token: str,
    state_dir: str | Path,
    executor: Callable[[TaskRecord], None] | None = None,
    committer: Callable[[TaskRecord], None] | None = None,
    spec_path: str | Path | None = None,
) -> ThreadingHTTPServer:
    app = AgentApp(
        controller_token,
        state_dir,
        executor=executor,
        committer=committer,
        spec_path=spec_path,
    )
    host, port = _parse_listen(listen)
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    server.daemon_threads = True
    server.agent_app = app  # type: ignore[attr-defined]
    return server


def _make_handler(app: AgentApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle_get()

        def do_POST(self) -> None:
            self._handle_post()

        def log_message(self, format: str, *args: object) -> None:
            if os.environ.get("SHARDFERRY_HTTP_DEBUG"):
                super().log_message(format, *args)
            return

        def _handle_get(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path in {"/v1/healthz", "/v1/readyz"}:
                self._json({"ok": True})
                return
            if path == "/v1/capabilities":
                self._json(app.capabilities())
                return
            if not self._authorized():
                return
            if path == "/v1/inventory":
                query = parse_qs(parsed.query)
                try:
                    rank = int(query.get("rank", [""])[0])
                except ValueError:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "inventory",
                        0,
                        "rank query parameter must be an integer",
                    )
                    return
                try:
                    self._json(app.inventory(rank))
                except (AgentError, ManifestError) as exc:
                    self._error(
                        404,
                        "SOURCE_NOT_FOUND",
                        "source",
                        False,
                        "inventory",
                        rank,
                        str(exc),
                    )
                return
            match = re.fullmatch(r"/v1/tasks/([^/]+)", path)
            if match:
                try:
                    self._json(app.store.get(unquote(match.group(1))).to_dict())
                except AgentStateError as exc:
                    self._error(
                        404,
                        "TARGET_CONFLICT",
                        "target",
                        False,
                        "task",
                        0,
                        str(exc),
                    )
                return
            self.send_error(404)

        def _handle_post(self) -> None:
            if not self._authorized():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/v1/plan":
                try:
                    body = self._body()
                    spec = body["spec"]
                    self._json(app.plan(spec))
                except (KeyError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "plan",
                        0,
                        str(exc),
                    )
                return
            if path == "/v1/tasks":
                try:
                    payload = self._body()
                    task = app.store.create(payload)
                    self._json(task.to_dict(), status=201)
                except (AgentStateError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "task",
                        0,
                        str(exc),
                    )
                return
            match = re.fullmatch(
                r"/v1/tasks/([^/]+)/(prepare|execute|commit|cancel)",
                path,
            )
            if not match:
                self.send_error(404)
                return
            try:
                # Go and other HTTP clients send an empty JSON object for
                # task actions. Consume it before the keep-alive connection
                # parses the next request line.
                self._body()
            except AgentStateError as exc:
                self._error(
                    400,
                    "INVALID_SPEC",
                    "input",
                    False,
                    "task",
                    0,
                    str(exc),
                )
                return
            task_id = unquote(match.group(1))
            action = match.group(2)
            try:
                task = app.store.get(task_id)
                if action == "prepare":
                    task = self._prepare(app.store, task)
                elif action == "execute":
                    task = self._execute(app, task)
                elif action == "commit":
                    task = self._commit(app.store, task)
                else:
                    task = self._cancel(app.store, task)
                self._json(task.to_dict())
            except AgentStateError as exc:
                self._error(
                    409,
                    "TARGET_CONFLICT",
                    "target",
                    False,
                    action,
                    task.rank if "task" in locals() else 0,
                    str(exc),
                )
            except AgentError as exc:
                self._error(
                    409,
                    "TARGET_CONFLICT",
                    "target",
                    exc.retryable,
                    action,
                    task.rank if "task" in locals() else 0,
                    str(exc),
                )

        def _prepare(self, store: TaskStore, task: TaskRecord) -> TaskRecord:
            if task.state is TaskPhase.CREATED:
                task = store.transition(task.task_id, TaskPhase.PREPARING)
            if task.state is TaskPhase.PREPARING:
                task = store.transition(task.task_id, TaskPhase.PREPARED)
            if task.state in {
                TaskPhase.PREPARED,
                TaskPhase.TRANSFERRING,
                TaskPhase.STAGED,
                TaskPhase.COMMIT_READY,
                TaskPhase.COMMITTING,
                TaskPhase.COMMITTED,
            }:
                return task
            raise AgentStateError(
                f"task {task.task_id} cannot be prepared from {task.state.value}"
            )

        def _execute(self, app: AgentApp, task: TaskRecord) -> TaskRecord:
            if task.state is TaskPhase.PREPARED:
                task = app.store.transition(
                    task.task_id, TaskPhase.TRANSFERRING
                )
            if task.state is TaskPhase.TRANSFERRING:
                try:
                    if app.executor is None:
                        raise AgentError("executor is not configured")
                    app.executor(task)
                    return app.store.transition(
                        task.task_id, TaskPhase.STAGED
                    )
                except Exception as exc:
                    app.store.fail(task.task_id, str(exc))
                    raise AgentError(str(exc), retryable=True) from exc
            if task.state in {
                TaskPhase.STAGED,
                TaskPhase.COMMIT_READY,
                TaskPhase.COMMITTING,
                TaskPhase.COMMITTED,
            }:
                return task
            raise AgentStateError(
                f"task {task.task_id} cannot be executed from {task.state.value}"
            )

        def _commit(self, store: TaskStore, task: TaskRecord) -> TaskRecord:
            if app.committer is None and task.state in {
                TaskPhase.STAGED,
                TaskPhase.COMMIT_READY,
                TaskPhase.COMMITTING,
                TaskPhase.COMMIT_INCOMPLETE,
            }:
                raise AgentError("committer is not configured")
            if task.state is TaskPhase.COMMIT_INCOMPLETE:
                task = store.transition(task.task_id, TaskPhase.COMMITTING)
            if task.state is TaskPhase.STAGED:
                task = store.transition(task.task_id, TaskPhase.COMMIT_READY)
            if task.state is TaskPhase.COMMIT_READY:
                task = store.transition(task.task_id, TaskPhase.COMMITTING)
            if task.state is TaskPhase.COMMITTING:
                try:
                    app.committer(task)
                    task = store.transition(
                        task.task_id, TaskPhase.COMMITTED
                    )
                except Exception as exc:
                    store.transition(
                        task.task_id,
                        TaskPhase.COMMIT_INCOMPLETE,
                        error=str(exc),
                    )
                    raise AgentError(str(exc), retryable=True) from exc
            if task.state is TaskPhase.COMMITTED:
                return task
            raise AgentStateError(
                f"task {task.task_id} cannot be committed from {task.state.value}"
            )

        def _cancel(self, store: TaskStore, task: TaskRecord) -> TaskRecord:
            if task.state in {
                TaskPhase.COMMITTING,
                TaskPhase.COMMITTED,
                TaskPhase.COMMIT_INCOMPLETE,
            }:
                raise AgentStateError("cannot cancel after commit started")
            if task.state is TaskPhase.CANCELLED:
                return task
            return store.transition(task.task_id, TaskPhase.CANCELLED)

        def _authorized(self) -> bool:
            if app.authorize(self.headers.get("Authorization")):
                return True
            self._error(
                401,
                "PERMISSION_DENIED",
                "control",
                False,
                "request",
                0,
                "missing or invalid bearer token",
            )
            return False

        def _body(self) -> dict[str, Any]:
            value = self.headers.get("Content-Length", "0")
            try:
                length = int(value)
            except ValueError as exc:
                raise AgentStateError("invalid Content-Length") from exc
            if length > 1024 * 1024:
                raise AgentStateError("request body is too large")
            data = self.rfile.read(length)
            try:
                payload = json.loads(data or b"{}")
            except ValueError as exc:
                raise AgentStateError("request body is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise AgentStateError("request body must be an object")
            return payload

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(
            self,
            status: int,
            code: str,
            category: str,
            retryable: bool,
            phase: str,
            rank: int,
            details: str,
        ) -> None:
            payload = {
                "version": "1",
                "code": code,
                "category": category,
                "retryable": retryable,
                "phase": phase,
                "rank": rank,
                "details": details,
            }
            try:
                validate_error(payload)
            except ValueError:
                payload["code"] = "INTERNAL"
                payload["category"] = "internal"
            self._json(payload, status=status)

    return Handler


def _parse_listen(listen: str) -> tuple[str, int]:
    parsed = urlsplit("//" + listen)
    if parsed.hostname is None or parsed.port is None:
        raise AgentError(f"invalid listen address: {listen}")
    return parsed.hostname, parsed.port


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
