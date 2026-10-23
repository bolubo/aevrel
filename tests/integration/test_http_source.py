from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

import numpy as np
import pytest
import yaml

from aevrel.executor import apply
from aevrel.planning.layout import DTYPE_BYTES
from aevrel.planning import plan
from aevrel.serve import build_server
from aevrel.remote import fetch_spec
from aevrel.storage.file import StorageError
from aevrel.storage.http import HttpStorage
from aevrel.storage.router import StorageRouter
from aevrel.planning.tensor_io import byte_runs
from aevrel.verification import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import assert_bitwise_equal, reference_reshard


def _start_server(spec: dict, tmp_path: Path, rank: int | None = None):
    source_spec = deepcopy(spec)
    spec_path = tmp_path / "server.yaml"
    spec_path.write_text(
        yaml.safe_dump(source_spec, sort_keys=False), encoding="utf-8"
    )
    server = build_server(str(spec_path), "0.0.0.0:0", rank=rank)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _plan_bytes_from_ranges(plan_payload: dict, spec: dict) -> int:
    source_map = {
        (shard["tensor"], shard["rank"]): shard
        for shard in spec["source"]["shards"]
    }
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    storage = StorageRouter()
    total = 0
    for node in plan_payload["nodes"]:
        tensor = tensors[node["tensor"]]
        bytes_per_item = DTYPE_BYTES[tensor["dtype"]]
        for source_input in node["inputs"]:
            shard = source_map[(node["tensor"], source_input["source_rank"])]
            local_offset = tuple(
                value - base
                for value, base in zip(
                    source_input["source_offset"], shard["offset"]
                )
            )
            runs = byte_runs(
                tuple(shard["size"]),
                local_offset,
                tuple(node["size"]),
                bytes_per_item,
            )
            for offset, size in runs:
                storage.read_range(shard["uri"], offset, size)
                total += size
    return total


def test_http_source_range_transfer_and_metrics(tmp_path) -> None:
    spec, arrays, tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    server, thread, port = _start_server(spec, tmp_path)
    host = _local_ip()
    try:
        client_spec = deepcopy(spec)
        for shard in client_spec["source"]["shards"]:
            shard["uri"] = (
                f"http://{host}:{port}/shard/"
                f"{quote(shard['tensor'], safe='')}/{shard['rank']}"
            )
        result = plan(client_spec)
        assert _plan_bytes_from_ranges(result, client_spec) == result["total_bytes"]

        metrics_before = json.loads(
            urlopen(f"http://{host}:{port}/metrics").read()
        )
        assert metrics_before["bytes_served"] == result["total_bytes"]

        apply(result, client_spec)
        verify(client_spec, result)
        expected = reference_reshard(
            tensor, (3, 1), ("Shard(0)", "Replicate()")
        )
        for rank, (_, expected_array) in expected.items():
            actual = np.frombuffer(
                (tmp_path / "target" / "w" / f"rank{rank}.bin").read_bytes(),
                dtype=np.float32,
            ).reshape(expected_array.shape)
            assert_bitwise_equal(actual, expected_array)

        metrics_after = json.loads(
            urlopen(f"http://{host}:{port}/metrics").read()
        )
        assert metrics_after["bytes_served"] >= result["total_bytes"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_http_source_unavailable_fails_before_target(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    server, thread, port = _start_server(spec, tmp_path)
    host = _local_ip()
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()

    client_spec = deepcopy(spec)
    for shard in client_spec["source"]["shards"]:
        shard["uri"] = (
            f"http://{host}:{port}/shard/"
            f"{quote(shard['tensor'], safe='')}/{shard['rank']}"
        )
    result = plan(client_spec)
    with pytest.raises(StorageError):
        apply(result, client_spec)
    assert not (tmp_path / "target" / "w" / "rank0.bin").exists()


def test_serve_cli_process_with_http_client(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    manifest = tmp_path / "server.yaml"
    manifest.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    probe = socket.socket()
    probe.bind(("", 0))
    port = probe.getsockname()[1]
    probe.close()
    host = _local_ip()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aevrel.cli",
            "serve",
            "--manifest",
            str(manifest),
            "--listen",
            f"0.0.0.0:{port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 5
        while True:
            try:
                urlopen(f"http://{host}:{port}/health", timeout=0.5).read()
                break
            except Exception:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)

        client_spec = deepcopy(spec)
        for shard in client_spec["source"]["shards"]:
            shard["uri"] = (
                f"http://{host}:{port}/shard/"
                f"{quote(shard['tensor'], safe='')}/{shard['rank']}"
            )
        result = plan(client_spec)
        apply(result, client_spec)
        verify(client_spec, result)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_http_failure_mid_apply_resumes(tmp_path, monkeypatch) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    server, thread, port = _start_server(spec, tmp_path)
    host = _local_ip()
    try:
        client_spec = deepcopy(spec)
        for shard in client_spec["source"]["shards"]:
            shard["uri"] = (
                f"http://{host}:{port}/shard/"
                f"{quote(shard['tensor'], safe='')}/{shard['rank']}"
            )
        result = plan(client_spec)
        original = HttpStorage.read_range
        calls = {"count": 0}

        def flaky_read_range(self, uri, offset, size):
            if calls["count"] >= 3:
                raise StorageError("injected network cut")
            calls["count"] += 1
            return original(self, uri, offset, size)

        monkeypatch.setattr(HttpStorage, "read_range", flaky_read_range)
        with pytest.raises(StorageError, match="injected"):
            apply(result, client_spec)
        monkeypatch.setattr(HttpStorage, "read_range", original)
        apply(result, client_spec)
        verify(client_spec, result)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_fetch_spec_from_agent(tmp_path, monkeypatch) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    server, thread, port = _start_server(spec, tmp_path)
    host = _local_ip()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        client_path = tmp_path / "client.yaml"
        client = fetch_spec(
            f"http://{host}:{port}",
            str(tmp_path / "fetched-target"),
            str(client_path),
        )
        assert all(
            shard["uri"].startswith(f"http://{host}:{port}/shard/")
            for shard in client["source"]["shards"]
        )
        result = plan(client)
        apply(result, client)
        verify(client, result)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_rank_scoped_source_agent_only_serves_its_rank(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    server, thread, port = _start_server(spec, tmp_path, rank=0)
    host = _local_ip()
    try:
        storage = StorageRouter()
        assert storage.exists(f"http://{host}:{port}/shard/w/0")
        assert not storage.exists(f"http://{host}:{port}/shard/w/1")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_multiple_rank_source_agents_with_rank_map(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "server.yaml"
    host = _local_ip()
    ports = []
    for _ in range(4):
        probe = socket.socket()
        probe.bind(("", 0))
        ports.append(probe.getsockname()[1])
        probe.close()
    endpoints = [f"http://{host}:{port}" for port in ports]
    spec["source"]["mesh"]["rank_map"] = [
        {"rank": rank, "endpoint": endpoint}
        for rank, endpoint in enumerate(endpoints)
    ]
    spec_path.write_text(
        yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
    )
    servers = []
    threads = []
    try:
        for rank, port in enumerate(ports):
            server = build_server(
                str(spec_path), f"0.0.0.0:{port}", rank=rank
            )
            thread = threading.Thread(
                target=server.serve_forever, daemon=True
            )
            thread.start()
            servers.append(server)
            threads.append(thread)
        client_path = tmp_path / "client.yaml"
        client = fetch_spec(
            endpoints[0],
            str(tmp_path / "target-multi-agent"),
            str(client_path),
        )
        plan_payload = plan(client)
        apply(plan_payload, client)
        verify(client, plan_payload)
        assert all(
            shard["uri"].split("/")[2] in {endpoint.split("/")[2] for endpoint in endpoints}
            for shard in client["source"]["shards"]
        )
    finally:
        for server in servers:
            server.shutdown()
        for thread in threads:
            thread.join(timeout=5)
        for server in servers:
            server.server_close()
