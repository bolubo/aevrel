from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from aevrel.dcp import export_dcp, inspect_dcp
from aevrel.executor import apply
from aevrel.planning import plan


pytestmark = pytest.mark.skipif(
    os.environ.get("AEVREL_RUN_DCP_DIST") != "1",
    reason="set AEVREL_RUN_DCP_DIST=1 to run the distributed DCP test",
)


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


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


def _run_torchrun(script: Path, args: list[str], workers: int) -> subprocess.CompletedProcess:
    torchrun = Path(sys.executable).with_name("torchrun")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    interface = _iface()
    if interface:
        env["GLOO_SOCKET_IFNAME"] = interface
    command = [
        str(torchrun),
        "--nnodes=1",
        f"--nproc_per_node={workers}",
        "--master_addr",
        _local_ip(),
        "--master_port",
        str(_free_port()),
        str(script),
        *args,
    ]
    return subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_real_dcp_4to3_loads_in_three_rank_target(tmp_path) -> None:
    pytest.importorskip("torch")
    source_dcp = tmp_path / "source.dcp"
    result = _run_torchrun(
        Path("tests/fixtures/dcp_worker.py"),
        ["--multi", "--out", str(source_dcp)],
        4,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    spec = inspect_dcp(source_dcp, tmp_path / "work", target_world_size=3)
    migrated_plan = plan(spec)
    apply(migrated_plan, spec)
    target_dcp = tmp_path / "target.dcp"
    export_dcp(spec, target_dcp)

    result = _run_torchrun(
        Path("tests/fixtures/dcp_load_worker.py"),
        ["--multi", "--model-output", "--ckpt", str(target_dcp)],
        3,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok=True" in result.stdout
