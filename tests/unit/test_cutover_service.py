from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover_service import CutoverService, build_cutover_server


def make_version(root: Path, version_id: str, data: bytes) -> None:
    version = root / version_id
    version.mkdir(parents=True)
    (version / "a.bin").write_bytes(data)
    write_marker(
        version,
        VersionMarker(
            version="1",
            version_id=version_id,
            job_uid="job-1",
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def test_cutover_service_follows_and_promotes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1-data")
    target = tmp_path / "target"
    service = CutoverService(
        source,
        target,
        owner="controller-a",
        epoch=1,
    )

    initial = service.status()
    assert initial["source_latest"] == "step-100"
    assert initial["target_latest"] is None
    assert service.follow_once()[0]["staged"] is True
    assert service.status()["target_latest"] == "step-100"

    result = service.cutover("step-100")
    assert result["state"] == "promoted"
    status = service.status()
    assert status["state"] == "promoted"
    assert status["current"]["version_id"] == "step-100"
    assert service.recover()["state"] == "promoted"


def test_cutover_service_http_requires_token(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1-data")
    service = CutoverService(source, tmp_path / "target")
    server = build_cutover_server(
        "0.0.0.0:0",
        service,
        "secret",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        try:
            urlopen(base + "/v1/status", timeout=5)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthenticated status must fail")
        request = Request(
            base + "/v1/status",
            headers={"Authorization": "Bearer secret"},
        )
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["source_latest"] == "step-100"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
