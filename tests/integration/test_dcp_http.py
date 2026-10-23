from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from shardferry.dcp import export_dcp, inspect_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.remote import fetch_spec
from shardferry.serve import build_server
from shardferry.verification import verify


pytestmark = pytest.mark.skipif(
    os.environ.get("SHARDFERRY_RUN_DCP_HTTP") != "1",
    reason="set SHARDFERRY_RUN_DCP_HTTP=1 to run the DCP HTTP test",
)


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _iface() -> str | None:
    for index, name in socket.if_nameindex():
        del index
        if name != "lo" and not name.startswith("loopback"):
            return name
    return None


def _run_torchrun(
    script: str,
    args: list[str],
    workers: int,
) -> subprocess.CompletedProcess:
    torchrun = Path(sys.executable).with_name("torchrun")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    interface = _iface()
    if interface:
        env["GLOO_SOCKET_IFNAME"] = interface
    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    return subprocess.run(
        [
            str(torchrun),
            "--nnodes=1",
            f"--nproc_per_node={workers}",
            "--master_addr",
            _local_ip(),
            "--master_port",
            str(port),
            str(Path(__file__).resolve().parents[2] / script),
            *args,
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dcp_over_http_4to3(tmp_path) -> None:
    pytest.importorskip("torch")
    source_dcp = tmp_path / "source.dcp"
    result = _run_torchrun(
        "tests/fixtures/dcp_worker.py",
        ["--multi", "--out", str(source_dcp)],
        4,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    work_dir = tmp_path / "work"
    inspect_dcp(source_dcp, work_dir, target_world_size=3)
    server = build_server(
        str(work_dir / "migration.yaml"),
        "0.0.0.0:0",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = _local_ip()
        port = server.server_address[1]
        client = fetch_spec(
            f"http://{host}:{port}",
            str(tmp_path / "target"),
            str(tmp_path / "client.yaml"),
        )
        plan_payload = plan(client)
        apply(plan_payload, client)
        verify(client, plan_payload)

        target_dcp = tmp_path / "target.dcp"
        export_dcp(client, target_dcp)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    result = _run_torchrun(
        "tests/fixtures/dcp_load_worker.py",
        ["--multi", "--model-output", "--ckpt", str(target_dcp)],
        3,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok=True" in result.stdout
