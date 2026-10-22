from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import yaml

from .identity import source_id_for_shard
from .manifest import load_spec
from .storage.file import FileStorage, StorageError


@dataclass(frozen=True)
class SourceEntry:
    path: Path
    size: int
    checksum: str


class _Counter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes_served = 0

    def add(self, size: int = 0) -> None:
        with self.lock:
            self.requests += 1
            self.bytes_served += size

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "requests": self.requests,
                "bytes_served": self.bytes_served,
            }

    def reset(self) -> None:
        with self.lock:
            self.requests = 0
            self.bytes_served = 0


def build_server(
    manifest_path: str,
    listen: str,
    rank: int | None = None,
    manifest_spec: dict | None = None,
) -> ThreadingHTTPServer:
    spec = load_spec(manifest_path)
    storage = FileStorage()
    sources: dict[tuple[str, int], SourceEntry] = {}
    sources_by_id: dict[str, SourceEntry] = {}
    for shard in spec["source"]["shards"]:
        if rank is not None and shard["rank"] != rank:
            continue
        uri = shard["uri"]
        if not storage.exists(uri):
            raise StorageError(f"source shard not found: {uri}")
        path = storage.path(uri)
        entry = SourceEntry(
            path=path,
            size=path.stat().st_size,
            checksum=storage.checksum(uri),
        )
        sources[(shard["tensor"], shard["rank"])] = entry
        sources_by_id[source_id_for_shard(shard)] = entry
    if rank is not None and not sources:
        raise StorageError(f"no source shards found for rank {rank}")
    host, port = _parse_listen(listen)
    handler = _make_handler(
        sources,
        sources_by_id,
        _Counter(),
        manifest_spec if manifest_spec is not None else _public_spec(spec),
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    manifest_path: str,
    listen: str,
    rank: int | None = None,
) -> None:
    server = build_server(manifest_path, listen, rank=rank)
    suffix = f" rank={rank}" if rank is not None else ""
    print(
        f"shardferry serve{suffix} listening on "
        f"{server.server_address[0]}:{server.server_address[1]}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _parse_listen(listen: str) -> tuple[str, int]:
    parsed = urlsplit("//" + listen)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"invalid listen address: {listen}")
    return parsed.hostname, parsed.port


def _make_handler(
    sources: dict[tuple[str, int], SourceEntry],
    sources_by_id: dict[str, SourceEntry],
    counter: _Counter,
    manifest_spec: dict,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_HEAD(self) -> None:
            self._handle(head=True)

        def do_GET(self) -> None:
            self._handle(head=False)

        def do_POST(self) -> None:
            if urlsplit(self.path).path == "/metrics/reset":
                counter.reset()
                self._json({"ok": True})
            else:
                self.send_error(404)

        def _handle(self, head: bool) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                counter.add()
                self._json({"ok": True}, head=head)
                return
            if parsed.path == "/metrics":
                counter.add()
                self._json(counter.snapshot(), head=head)
                return
            if parsed.path == "/manifest":
                counter.add()
                data = yaml.safe_dump(manifest_spec, sort_keys=False).encode(
                    "utf-8"
                )
                self._bytes(data, "application/yaml", head=head)
                return
            if not parsed.path.startswith("/shard/"):
                self.send_error(404)
                return

            rest = parsed.path[len("/shard/") :]
            parts = rest.split("/")
            if len(parts) not in {2, 3}:
                self.send_error(404)
                return
            try:
                key = (unquote(parts[0]), int(parts[1]))
            except ValueError:
                self.send_error(404)
                return
            if len(parts) == 3:
                entry = sources_by_id.get(unquote(parts[2]))
            else:
                entry = sources.get(key)
            if entry is None:
                self.send_error(404)
                return

            query = parse_qs(parsed.query)
            try:
                offset = int(query.get("offset", ["0"])[0])
                length = int(query.get("length", [str(entry.size)])[0])
            except ValueError:
                self.send_error(400)
                return
            if offset < 0 or length < 0 or offset + length > entry.size:
                self.send_error(416)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Shard-SHA256", entry.checksum)
            self.end_headers()
            if head or length == 0:
                counter.add()
                return
            try:
                with entry.path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(length)
                self.wfile.write(data)
                counter.add(len(data))
            except (BrokenPipeError, ConnectionResetError):
                counter.add()

        def _json(self, payload: dict, head: bool = False) -> None:
            data = (json.dumps(payload) + "\n").encode("utf-8")
            self._bytes(data, "application/json", head=head)

        def _bytes(self, data: bytes, content_type: str, head: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _public_spec(spec: dict) -> dict:
    public = deepcopy(spec)
    public["source"]["storage"]["uri"] = "served"
    public["target"]["storage"]["uri"] = "target"
    for shard in public["source"]["shards"]:
        identifier = source_id_for_shard(shard)
        shard["id"] = identifier
        shard["uri"] = (
            f"/shard/{quote(shard['tensor'], safe='')}/{shard['rank']}/"
            f"{quote(identifier, safe=':')}"
        )
    return public
