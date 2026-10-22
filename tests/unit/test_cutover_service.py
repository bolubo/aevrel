from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover_service import CutoverService, build_cutover_server


def make_version(
    root: Path,
    version_id: str,
    data: bytes,
    *,
    parent_id: str | None = None,
) -> None:
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
            parent_id=parent_id,
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


def test_cutover_service_auto_follows_in_background(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1-data")
    service = CutoverService(
        source,
        tmp_path / "target",
        follow_interval=0.01,
        max_versions_per_cycle=1,
    )
    assert service.chunking.algorithm == "file"
    service.start_following()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = service.status()
            if (
                status["target_latest"] == "step-100"
                and status["follow_state"] == "following"
            ):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("background follower did not catch up")

        assert status["follow_state"] == "following"
        assert status["lag_versions"] == 0
        assert status["follow_synced_versions"] == 1

        make_version(
            source,
            "step-200",
            b"v2-data",
            parent_id="step-100",
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = service.status()
            if (
                status["target_latest"] == "step-200"
                and status["follow_state"] == "following"
            ):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("background follower did not follow new version")

        assert status["follow_state"] == "following"
        assert status["lag_versions"] == 0
        assert status["follow_synced_versions"] == 2
    finally:
        service.stop_following()

    assert service.follow_loop.status().state == "stopped"
    service.stop_following()


def test_cutover_service_persists_last_cutover(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1-data")
    target = tmp_path / "target"
    service = CutoverService(source, target)
    service.follow_once()
    service.cutover("step-100")

    reopened = CutoverService(source, target)
    last = reopened.status()["last_cutover"]
    assert last["version_id"] == "step-100"
    assert last["target_ready_seconds"] >= 0


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
