from __future__ import annotations

import json
import socket
import threading
from urllib.request import Request, urlopen

from shardferry.agent import build_agent_server
from shardferry.planning import validate_plan
from tests.fixtures.generated import generate_sharded_spec


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def test_plan_endpoint_returns_valid_plan(tmp_path) -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/plan",
            data=json.dumps({"version": "1", "spec": spec}).encode("utf-8"),
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        payload = json.loads(urlopen(request).read())
        validate_plan(payload["plan"], spec)
        assert payload["plan"]["nodes"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
