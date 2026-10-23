from __future__ import annotations

import json
import socket
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

from aevrel.agent import build_agent_server
from aevrel.contracts import validate_inventory
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


def test_inventory_endpoint_returns_rank_chunks(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["checkpoint"]["format"] = "dcp"
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        spec_path=str(spec_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/inventory?rank=0",
            headers={"Authorization": "Bearer secret"},
        )
        payload = json.loads(urlopen(request).read())
        validate_inventory(payload)
        assert payload["rank"] == 0
        assert payload["format"] == "dcp"
        assert payload["tensors"]
        assert payload["tensors"][0]["name"] == "w"
        assert payload["chunks"]
        assert payload["chunks"][0]["checksum"].startswith("sha256:")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_inventory_unknown_rank_is_404(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["checkpoint"]["format"] = "dcp"
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        spec_path=str(spec_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/inventory?rank=999",
            headers={"Authorization": "Bearer secret"},
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("unknown rank must be rejected")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_agent_rank_discovery_for_spec_source(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["checkpoint"]["format"] = "dcp"
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        spec_path=str(spec_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/ranks",
            headers={"Authorization": "Bearer secret"},
        )
        payload = json.loads(urlopen(request).read())
        assert payload == {"version": "1", "ranks": [0, 1, 2, 3]}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
