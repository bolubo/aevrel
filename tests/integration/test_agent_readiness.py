from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from aevrel.agent import build_agent_server
from aevrel.dcp import export_dcp
from aevrel.executor import apply
from aevrel.planning import plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _source_dcp(tmp_path: Path) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "source.dcp"
    export_dcp(spec, output)
    return output


def test_agent_exposes_readonly_checkpoint_record(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _source_dcp(tmp_path)
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url="http://source",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/checkpoint-record",
            headers={"Authorization": "Bearer secret"},
        )
        payload = json.loads(urlopen(request).read())
        assert payload["record"]["complete"] is True
        assert len(payload["record"]["ranks"]) == 4
        assert payload["record"]["provider"] == "dcp"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
