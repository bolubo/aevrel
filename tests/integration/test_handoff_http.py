from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
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


def test_handoff_http_warm_target_and_final_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    make_version(source, "step-100", b"v1")
    ready_versions: list[str] = []

    class FinalCheckpointHook:
        def pause(self, version_id: str) -> None:
            del version_id
            make_version(source, "step-200", b"v2")

        def resume(self) -> None:
            return

    service = CutoverService(
        source,
        target,
        pause_hook=FinalCheckpointHook(),
        target_ready=ready_versions.append,
        follow_interval=0.01,
        max_versions_per_cycle=1,
    )
    server = build_cutover_server("0.0.0.0:0", service, "secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service.start_following()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if service.status()["target_latest"] == "step-100":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("warm target did not stage the first version")

        request = Request(
            f"http://{_local_ip()}:{server.server_address[1]}/v1/cutover",
            data=json.dumps({}).encode("utf-8"),
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
        assert result["state"] == "promoted"
        assert result["version_id"] == "step-200"
        assert ready_versions == ["step-200"]
        assert service.status()["current"]["version_id"] == "step-200"
    finally:
        service.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
