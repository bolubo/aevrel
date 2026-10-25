from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import prod
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from .state import AgentStateError, TaskPhase, TaskRecord, TaskStore
from ..contracts import (
    contract_version,
    validate_capabilities,
    validate_error,
    validate_ranks,
)
from ..dcp import (
    DcpChunkRef,
    inventory_dcp_descriptors,
    local_dcp_ranks,
    read_dcp_chunk,
    read_dcp_chunk_range,
)
from ..planning.layout import DTYPE_BYTES
from ..manifest import ManifestError, load_spec
from ..planning import plan as build_plan
from ..verification import ProofError, combine_proofs
from ..gpu_transport import (
    RANGE_ACCEPT_HEADER,
    RANGE_ENCODING_HEADER,
    RANGE_RAW_LENGTH_HEADER,
    RangeCodec,
    encode_response,
    make_range_codec_from_env,
)
from ..gpu_batch import AdaptiveBatchCodec, BATCH_ENCODING


MAX_METADATA_BYTES = 32 * 1024 * 1024


def resume_state_summary(path: Path | None) -> dict[str, Any] | None:
    """Summarize the resume-state manifest this agent can see.

    ``None`` means "this agent has no closure evidence", which the controller
    must treat as an absence of proof rather than an incomplete checkpoint.
    A manifest that exists but does not parse still produces a summary with
    ``closure: MALFORMED`` and no digest, so the failure stays visible
    instead of silently degrading to "no evidence".
    """
    if path is None:
        return None
    from ..state.resume_state import classify_resume_state

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(f"could not read resume-state manifest {path}: {exc}") from exc
    report = classify_resume_state(payload)
    manifest = report.manifest
    return {
        "closure": report.status,
        "manifestDigest": report.manifest_digest or "",
        "components": [
            {
                "kind": component.kind,
                "required": component.required,
                "present": component.present,
            }
            for component in (manifest.components if manifest is not None else ())
        ],
        "reasonCodes": [reason.code for reason in report.reasons],
    }


class AgentError(RuntimeError):
    """Raised when an agent cannot serve or mutate a task."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class _ShardCache:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, max_bytes)
        self._items: dict[str, bytes] = {}
        self._order: list[str] = []
        self._size = 0

    def get(self, key: str) -> bytes | None:
        value = self._items.get(key)
        if value is not None:
            self._order.remove(key)
            self._order.append(key)
        return value

    def put(self, key: str, value: bytes) -> None:
        if len(value) > self.max_bytes:
            raise AgentError(
                f"source chunk {key} exceeds cache limit {self.max_bytes}"
            )
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= len(old)
            self._order.remove(key)
        while self._items and self._size + len(value) > self.max_bytes:
            oldest = self._order.pop(0)
            self._size -= len(self._items.pop(oldest))
        self._items[key] = value
        self._order.append(key)
        self._size += len(value)


class AgentApp:
    def __init__(
        self,
        controller_token: str,
        state_dir: str | Path,
        executor: Callable[[TaskRecord], None] | None = None,
        committer: Callable[[TaskRecord], None] | None = None,
        spec_path: str | Path | None = None,
        dcp_checkpoint: str | Path | None = None,
        dcp_metadata: str | Path | None = None,
        target_root: str | Path | None = None,
        public_url: str | None = None,
        max_cache_bytes: int = 512 * 1024 * 1024,
        range_codec: RangeCodec | None = None,
        resume_state_path: str | Path | None = None,
    ) -> None:
        if not controller_token:
            raise AgentError("controller token must not be empty")
        if spec_path is not None and dcp_checkpoint is not None:
            raise AgentError(
                "spec_path and dcp_checkpoint are mutually exclusive"
            )
        self.controller_token = controller_token
        self.store = TaskStore(state_dir)
        self.executor = executor
        self.committer = committer
        self.spec_path = Path(spec_path).resolve() if spec_path else None
        self.spec = load_spec(self.spec_path) if self.spec_path else None
        self.dcp_checkpoint = (
            Path(dcp_checkpoint).resolve() if dcp_checkpoint else None
        )
        self.dcp_metadata = (
            Path(dcp_metadata).resolve() if dcp_metadata else None
        )
        self._installed_metadata: Path | None = None
        self.target_root = (
            Path(target_root).resolve() if target_root else None
        )
        self.public_url = (public_url or "http://127.0.0.1:9443").rstrip("/")
        self.resume_state_path = (
            Path(resume_state_path).resolve() if resume_state_path else None
        )
        self.range_codec = (
            range_codec
            if range_codec is not None
            else make_range_codec_from_env()
        )
        self.batch_codec = (
            AdaptiveBatchCodec(self.range_codec.adaptive)
            if self.range_codec is not None
            else None
        )
        self._shards: dict[str, DcpChunkRef] = {}
        self._cache = _ShardCache(max_cache_bytes)
        self._metadata_hash = (
            _sha256(self.spec_path.read_bytes())
            if self.spec_path is not None
            else None
        )

    def capabilities(self) -> dict[str, Any]:
        features = [
            "dcp",
            "range-read",
            "bitwise-verify",
            "merkle-proof",
            "adaptive-rate-limit",
        ]
        if self.spec_path is not None or self.dcp_checkpoint is not None:
            features.append("rank-discovery")
        if self.dcp_checkpoint is not None:
            features.append("metadata-transfer")
            features.append("checkpoint-readiness")
        if self.target_root is not None:
            features.append("target-preflight")
        if self.range_codec is not None:
            features.append("range-codec")
        payload = {
            "version": contract_version(),
            "apiVersion": "aevrel.dev/v1",
            "agentVersion": __version__,
            "roles": ["source", "target"],
            "features": features,
        }
        validate_capabilities(payload)
        return payload

    def preflight(
        self,
        target_root: str | Path,
        *,
        required_bytes: int = 0,
    ) -> dict[str, Any]:
        if self.target_root is None:
            raise AgentError("target root is not configured")
        if required_bytes < 0:
            raise AgentError("required bytes must not be negative")
        root = Path(target_root).resolve()
        try:
            root.relative_to(self.target_root)
        except ValueError as exc:
            raise AgentError(
                f"target root {root} is outside {self.target_root}"
            ) from exc
        existing = root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not existing.is_dir():
            raise AgentError(f"target parent is not a directory: {existing}")
        if not os.access(existing, os.W_OK):
            raise AgentError(f"target parent is not writable: {existing}")
        if root.exists():
            if not root.is_dir():
                raise AgentError(f"target root is not a directory: {root}")
            if not os.access(root, os.W_OK):
                raise AgentError(f"target root is not writable: {root}")
        free_bytes = shutil.disk_usage(existing).free
        if required_bytes > free_bytes:
            raise AgentError(
                f"target free space {free_bytes} < required {required_bytes}"
            )
        return {
            "ok": True,
            "target_root": str(root),
            "existing_parent": str(existing),
            "free_bytes": free_bytes,
            "required_bytes": required_bytes,
            "nonempty": bool(root.exists() and any(root.iterdir())),
        }

    def cleanup_target(
        self,
        target_root: str | Path,
        *,
        purge_target: bool = False,
    ) -> dict[str, Any]:
        if self.target_root is None:
            raise AgentError("target root is not configured")
        root = Path(target_root).resolve()
        try:
            root.relative_to(self.target_root)
        except ValueError as exc:
            raise AgentError(
                f"target root {root} is outside {self.target_root}"
            ) from exc
        if root == self.target_root:
            raise AgentError("refusing to remove the configured target root")
        staging = root / ".aevrel"
        if staging.exists():
            shutil.rmtree(staging)
        purged = False
        if purge_target and root.exists():
            shutil.rmtree(root)
            purged = True
        return {
            "ok": True,
            "target_root": str(root),
            "staging_removed": True,
            "target_purged": purged,
        }

    def inventory(self, rank: int) -> dict[str, Any]:
        if self.dcp_checkpoint is not None:
            inventory, refs = inventory_dcp_descriptors(
                self.dcp_checkpoint,
                rank,
                metadata_dir=self._metadata_dir(),
                file_prefix=self.public_url + "/v1/shards",
            )
            self._shards.update(refs)
            return inventory
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

    def checkpoint_record(
        self,
        *,
        namespace: str = "",
        job_uid: str = "",
        version: str = "",
        step: int | None = None,
    ) -> dict[str, Any]:
        if self.dcp_checkpoint is None:
            raise AgentError(
                "checkpoint readiness requires dcp_checkpoint"
            )
        from ..local.dcp import DCPStateProvider
        from ..local.errors import LocalError

        try:
            record = DCPStateProvider().inventory(
                self.dcp_checkpoint,
                namespace=namespace,
                job_uid=job_uid,
                version=version,
                step=step,
            )
        except LocalError as exc:
            raise AgentError(str(exc)) from exc
        return {
            "version": "2",
            "record": record.to_dict(),
            "resumeState": resume_state_summary(self.resume_state_path),
        }

    def ranks(self) -> dict[str, Any]:
        if self.dcp_checkpoint is not None:
            ranks = local_dcp_ranks(
                self.dcp_checkpoint,
                metadata_dir=self._optional_metadata_dir(),
                allow_missing_metadata=True,
            )
        elif self.spec is not None:
            ranks = sorted({
                int(shard["rank"])
                for shard in self.spec["source"]["shards"]
            })
        else:
            raise AgentError("rank discovery is not configured")
        payload = {"version": contract_version(), "ranks": ranks}
        validate_ranks(payload)
        return payload

    def metadata(self) -> tuple[bytes, str]:
        """Return the DCP metadata visible on this agent."""
        if self.dcp_checkpoint is None:
            raise AgentError("DCP metadata is not configured")
        path = self._metadata_dir() / ".metadata"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AgentError(f"could not read DCP metadata: {exc}") from exc
        return data, _sha256(data)

    def install_metadata(self, data: bytes) -> dict[str, Any]:
        """Install metadata fetched from the configured owner rank.

        Only the small DCP metadata file travels to peer source agents. Shard
        bytes continue to be read from each agent's local checkpoint root.
        """
        if self.dcp_checkpoint is None:
            raise AgentError("DCP metadata is not configured")
        if not data:
            raise AgentError("DCP metadata must not be empty")
        if len(data) > MAX_METADATA_BYTES:
            raise AgentError(
                f"DCP metadata exceeds {MAX_METADATA_BYTES} bytes"
            )
        digest = _sha256(data)
        root = (
            self.store.root
            / "metadata"
            / digest.removeprefix("sha256:")
        )
        root.mkdir(parents=True, exist_ok=True)
        path = root / ".metadata"
        if not path.is_file() or _sha256(path.read_bytes()) != digest:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".metadata-",
                dir=root,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        self._installed_metadata = root
        return {
            "ok": True,
            "metadata_hash": digest,
            "bytes": len(data),
        }

    def _optional_metadata_dir(self) -> Path | None:
        for root in (
            self.dcp_metadata,
            self.dcp_checkpoint,
            self._installed_metadata,
        ):
            if root is not None and (root / ".metadata").is_file():
                return root
        return None

    def _metadata_dir(self) -> Path:
        root = self._optional_metadata_dir()
        if root is None:
            raise AgentError(
                "DCP metadata is not visible; fetch it from the "
                "metadataOwnerRank agent and install it first"
            )
        return root

    def read_shard(
        self,
        chunk_hash: str,
        offset: int,
        length: int,
    ) -> tuple[bytes, str]:
        ref = self._shards.get(chunk_hash)
        if ref is None:
            raise AgentError(f"source shard not found: {chunk_hash}")
        if ref.data_size:
            try:
                data = read_dcp_chunk_range(
                    self.dcp_checkpoint,
                    ref,
                    offset,
                    length,
                )
            except Exception as exc:
                raise AgentError(str(exc)) from exc
            return data, ref.checksum
        data = self._cache.get(chunk_hash)
        if data is None:
            try:
                data = read_dcp_chunk(self.dcp_checkpoint, ref)
            except Exception as exc:
                raise AgentError(str(exc)) from exc
            self._cache.put(chunk_hash, data)
        if offset < 0 or length < 0 or offset + length > len(data):
            raise AgentError("source shard range is out of bounds")
        return data[offset : offset + length], _sha256(data)

    def shard_info(self, chunk_hash: str) -> tuple[int, str]:
        ref = self._shards.get(chunk_hash)
        if ref is None:
            raise AgentError(f"source shard not found: {chunk_hash}")
        return ref.size, ref.checksum

    def encode_shard_batch(self, ranges: list[dict[str, Any]]) -> bytes:
        if self.batch_codec is None:
            raise AgentError("batch codec is not configured")
        if not isinstance(ranges, list) or not ranges:
            raise AgentError("batch ranges must be a non-empty list")
        chunks = []
        for item in ranges:
            try:
                chunk_hash = str(item["chunk"])
                offset = int(item["offset"])
                length = int(item["length"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentError("invalid batch range") from exc
            data, _ = self.read_shard(chunk_hash, offset, length)
            chunks.append(data)
        return self.batch_codec.encode(chunks).frame

    def plan(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": contract_version(),
            "plan": build_plan(spec),
        }

    def recover_tasks(self) -> None:
        """Reconcile durable task state after an agent restart."""
        if self.committer is None:
            return
        for task in self.store.list():
            if task.state is TaskPhase.TRANSFERRING:
                try:
                    self.store.transition(
                        task.task_id,
                        TaskPhase.PREPARED,
                    )
                except AgentStateError:
                    continue
                continue
            if task.state not in {
                TaskPhase.COMMITTING,
                TaskPhase.COMMIT_INCOMPLETE,
            }:
                continue
            try:
                if task.state is TaskPhase.COMMIT_INCOMPLETE:
                    task = self.store.transition(
                        task.task_id,
                        TaskPhase.COMMITTING,
                    )
                with self.store.commit_guard(task.task_id):
                    result = self.committer(task)
                    if isinstance(result, dict):
                        self.store.set_report(task.task_id, result)
                    self.store.transition(task.task_id, TaskPhase.COMMITTED)
            except AgentStateError as exc:
                if "stale commit token" in str(exc):
                    continue
                if task.state not in {
                    TaskPhase.COMMITTED,
                    TaskPhase.FAILED,
                    TaskPhase.CANCELLED,
                }:
                    try:
                        self.store.transition(
                            task.task_id,
                            TaskPhase.COMMIT_INCOMPLETE,
                            error=str(exc),
                        )
                    except AgentStateError:
                        pass
            except Exception as exc:
                if task.state not in {
                    TaskPhase.COMMITTED,
                    TaskPhase.FAILED,
                    TaskPhase.CANCELLED,
                }:
                    try:
                        self.store.transition(
                            task.task_id,
                            TaskPhase.COMMIT_INCOMPLETE,
                            error=str(exc),
                        )
                    except AgentStateError:
                        pass

    def authorize(self, header: str | None) -> bool:
        return header == f"Bearer {self.controller_token}"


def build_agent_server(
    listen: str,
    controller_token: str,
    state_dir: str | Path,
    executor: Callable[[TaskRecord], None] | None = None,
    committer: Callable[[TaskRecord], None] | None = None,
    spec_path: str | Path | None = None,
    dcp_checkpoint: str | Path | None = None,
    dcp_metadata: str | Path | None = None,
        target_root: str | Path | None = None,
    public_url: str | None = None,
    max_cache_bytes: int = 512 * 1024 * 1024,
    range_codec: RangeCodec | None = None,
    resume_state_path: str | Path | None = None,
    tls_cert: str | Path | None = None,
    tls_key: str | Path | None = None,
    tls_ca: str | Path | None = None,
) -> ThreadingHTTPServer:
    app = AgentApp(
        controller_token,
        state_dir,
        executor=executor,
        committer=committer,
        spec_path=spec_path,
        dcp_checkpoint=dcp_checkpoint,
        dcp_metadata=dcp_metadata,
        target_root=target_root,
        public_url=public_url,
        max_cache_bytes=max_cache_bytes,
        range_codec=range_codec,
        resume_state_path=resume_state_path,
    )
    app.recover_tasks()
    host, port = _parse_listen(listen)
    server = ThreadingHTTPServer((host, port), _make_handler(app))
    server.daemon_threads = True
    server.agent_app = app  # type: ignore[attr-defined]
    if tls_cert or tls_key:
        if not tls_cert or not tls_key:
            raise AgentError("TLS cert and key are required together")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls_cert), str(tls_key))
        if tls_ca:
            context.load_verify_locations(str(tls_ca))
            context.verify_mode = ssl.CERT_REQUIRED
        server.socket = context.wrap_socket(
            server.socket,
            server_side=True,
        )
    return server


def _make_handler(app: AgentApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle_get()

        def do_HEAD(self) -> None:
            self._handle_get(head=True)

        def do_POST(self) -> None:
            self._handle_post()

        def log_message(self, format: str, *args: object) -> None:
            if os.environ.get("AEVREL_HTTP_DEBUG"):
                super().log_message(format, *args)
            return

        def _handle_get(self, head: bool = False) -> None:
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
            if path == "/v1/metrics":
                self._json(app.metrics.snapshot())
                return
            if path == "/v1/metadata":
                try:
                    data, digest = app.metadata()
                except AgentError as exc:
                    self._error(
                        404,
                        "SOURCE_NOT_FOUND",
                        "source",
                        False,
                        "metadata",
                        0,
                        str(exc),
                    )
                    return
                self._bytes(
                    data,
                    content_type="application/octet-stream",
                    headers={"X-Metadata-SHA256": digest},
                    head=head,
                )
                return
            if path.startswith("/v1/shards/"):
                self._shard(
                    unquote(path[len("/v1/shards/") :]),
                    parse_qs(parsed.query),
                    head=head,
                )
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
            if path == "/v1/ranks":
                try:
                    self._json(app.ranks())
                except (AgentError, ManifestError, ValueError) as exc:
                    self._error(
                        404,
                        "SOURCE_NOT_FOUND",
                        "source",
                        False,
                        "inventory",
                        0,
                        str(exc),
                    )
                return
            if path == "/v1/checkpoint-record":
                query = parse_qs(parsed.query)
                try:
                    step_value = query.get("step", [""])[0]
                    step = int(step_value) if step_value else None
                    self._json(
                        app.checkpoint_record(
                            namespace=query.get("namespace", [""])[0],
                            job_uid=query.get("job_uid", [""])[0],
                            version=query.get("version", [""])[0],
                            step=step,
                        )
                    )
                except (AgentError, ValueError) as exc:
                    self._error(
                        404,
                        "SOURCE_NOT_FOUND",
                        "source",
                        False,
                        "checkpoint-record",
                        0,
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

        def _shard(
            self,
            chunk_hash: str,
            query: dict[str, list[str]],
            *,
            head: bool,
        ) -> None:
            try:
                offset = int(query.get("offset", ["0"])[0])
                length = int(query["length"][0]) if "length" in query else None
            except ValueError:
                self.send_error(400)
                return
            try:
                size, checksum = app.shard_info(chunk_hash)
                if length is None:
                    length = size
                data = b""
                full_checksum = checksum
                if not head:
                    data, full_checksum = app.read_shard(
                        chunk_hash,
                        offset,
                        length,
                    )
            except AgentError:
                self.send_error(404)
                return
            raw_data = data
            response = encode_response(
                raw_data,
                app.range_codec,
                self.headers.get(RANGE_ACCEPT_HEADER, ""),
                head=head,
                raw_checksum=full_checksum if head else None,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Length",
                str(length if head else len(response.data)),
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Shard-SHA256", checksum)
            self.send_header("X-Range-SHA256", response.raw_checksum)
            if response.encoding is not None:
                self.send_header(RANGE_ENCODING_HEADER, response.encoding)
                self.send_header(
                    RANGE_RAW_LENGTH_HEADER,
                    str(response.raw_length),
                )
            self.end_headers()
            if not head:
                self.wfile.write(response.data)

        def _handle_post(self) -> None:
            if not self._authorized():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/v1/metadata":
                try:
                    data = self._raw_body(max_bytes=MAX_METADATA_BYTES)
                    self._json(app.install_metadata(data))
                except (AgentError, AgentStateError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "metadata",
                        0,
                        str(exc),
                    )
                return
            if path == "/v1/shards/batch":
                try:
                    body = self._body()
                    frame = app.encode_shard_batch(body["ranges"])
                except (KeyError, AgentError, TypeError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "batch",
                        0,
                        str(exc),
                    )
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/octet-stream",
                )
                self.send_header("Content-Length", str(len(frame)))
                self.send_header(
                    RANGE_ENCODING_HEADER,
                    BATCH_ENCODING,
                )
                self.end_headers()
                self.wfile.write(frame)
                return
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
            if path == "/v1/preflight":
                try:
                    body = self._body()
                    target_root = body["target_root"]
                    required_bytes = int(body.get("required_bytes", 0))
                    self._json(
                        app.preflight(
                            target_root,
                            required_bytes=required_bytes,
                        )
                    )
                except (KeyError, AgentError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "preflight",
                        0,
                        str(exc),
                    )
                return
            if path == "/v1/cleanup":
                try:
                    body = self._body()
                    target_root = body["target_root"]
                    purge_target = bool(body.get("purge_target", False))
                    self._json(
                        app.cleanup_target(
                            target_root,
                            purge_target=purge_target,
                        )
                    )
                except (KeyError, AgentError, OSError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "cleanup",
                        0,
                        str(exc),
                    )
                return
            if path == "/v1/proof/merge":
                try:
                    body = self._body()
                    proofs = body["proofs"]
                    if not isinstance(proofs, list):
                        raise ValueError("proofs must be a list")
                    self._json({"proof": combine_proofs(proofs)})
                except (KeyError, ProofError, ValueError) as exc:
                    self._error(
                        400,
                        "INVALID_SPEC",
                        "input",
                        False,
                        "proof",
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
                    result = app.executor(task)
                    if isinstance(result, dict):
                        app.store.set_report(task.task_id, result)
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
                with store.commit_guard(task.task_id):
                    try:
                        result = app.committer(task)
                        if isinstance(result, dict):
                            store.set_report(task.task_id, result)
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
            if os.environ.get("AEVREL_HTTP_DEBUG"):
                print(
                    f"HTTP {self.command} {self.path} auth="
                    f"{self.headers.get('Authorization')}",
                    flush=True,
                )
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
            data = self._raw_body(max_bytes=1024 * 1024)
            try:
                payload = json.loads(data or b"{}")
            except ValueError as exc:
                raise AgentStateError("request body is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise AgentStateError("request body must be an object")
            return payload

        def _raw_body(self, *, max_bytes: int) -> bytes:
            value = self.headers.get("Content-Length", "0")
            try:
                length = int(value)
            except ValueError as exc:
                raise AgentStateError("invalid Content-Length") from exc
            if length < 0:
                raise AgentStateError("invalid Content-Length")
            if length > max_bytes:
                raise AgentStateError("request body is too large")
            data = self.rfile.read(length)
            if len(data) != length:
                raise AgentStateError("short request body")
            return data

        def _bytes(
            self,
            payload: bytes,
            *,
            content_type: str,
            headers: dict[str, str] | None = None,
            head: bool = False,
        ) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                self.wfile.write(payload)

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
