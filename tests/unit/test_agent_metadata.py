from __future__ import annotations

import hashlib
import json
import socket
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shardferry.agent import build_agent_server
from shardferry.agent.server import AgentApp


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _serve(tmp_path: Path, checkpoint: Path):
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url="http://source",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{_local_ip()}:{server.server_address[1]}"
    return server, thread, base


def test_source_agent_can_start_without_shared_metadata_and_install_it(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "local-shard"
    checkpoint.mkdir()
    (checkpoint / "__2_0.distcp").write_bytes(b"local shard")
    server, thread, base = _serve(tmp_path, checkpoint)
    try:
        ranks = json.loads(
            urlopen(
                Request(
                    base + "/v1/ranks",
                    headers={"Authorization": "Bearer secret"},
                )
            ).read()
        )
        assert ranks == {"version": "1", "ranks": [2]}

        try:
            urlopen(
                Request(
                    base + "/v1/metadata",
                    headers={"Authorization": "Bearer secret"},
                )
            )
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("missing metadata must not be served")

        metadata = b"metadata-from-owner-rank-0"
        digest = "sha256:" + hashlib.sha256(metadata).hexdigest()
        installed = json.loads(
            urlopen(
                Request(
                    base + "/v1/metadata",
                    data=metadata,
                    method="POST",
                    headers={
                        "Authorization": "Bearer secret",
                        "Content-Type": "application/octet-stream",
                    },
                )
            ).read()
        )
        assert installed == {
            "ok": True,
            "metadata_hash": digest,
            "bytes": len(metadata),
        }

        response = urlopen(
            Request(
                base + "/v1/metadata",
                headers={"Authorization": "Bearer secret"},
            )
        )
        assert response.read() == metadata
        assert response.headers["X-Metadata-SHA256"] == digest
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_metadata_install_rejects_empty_body(tmp_path: Path) -> None:
    checkpoint = tmp_path / "local-shard"
    checkpoint.mkdir()
    (checkpoint / "__0_0.distcp").write_bytes(b"local shard")
    server, thread, base = _serve(tmp_path, checkpoint)
    try:
        request = Request(
            base + "/v1/metadata",
            data=b"",
            method="POST",
            headers={"Authorization": "Bearer secret"},
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read())
            assert "empty" in payload["details"]
        else:
            raise AssertionError("empty metadata must be rejected")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_owner_prefers_current_checkpoint_metadata_over_old_install(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "local-shard"
    checkpoint.mkdir()
    (checkpoint / "__0_0.distcp").write_bytes(b"local shard")
    app = AgentApp(
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
    )
    app.install_metadata(b"old metadata")
    current = b"current checkpoint metadata"
    (checkpoint / ".metadata").write_bytes(current)

    data, digest = app.metadata()

    assert data == current
    assert digest == "sha256:" + hashlib.sha256(current).hexdigest()
