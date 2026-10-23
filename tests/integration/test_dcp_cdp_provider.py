from __future__ import annotations

import socket
import threading

import pytest

from aevrel.agent import build_agent_server
from aevrel.cdp import (
    CheckpointFollower,
    ChunkingPolicy,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from aevrel.dcp import export_dcp
from aevrel.dcp.provider import DcpRangeSpecBuilder
from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.target_layout import DirectIncrementalApplyFileProvider
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


def _local_ip() -> str:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("1.1.1.1", 80))
        return connection.getsockname()[0]
    finally:
        connection.close()


def test_dcp_range_spec_uses_source_agent_urls(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, source_arrays, _tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, source_arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "source.dcp"
    export_dcp(spec, checkpoint)

    builder = DcpRangeSpecBuilder(
        source_world=2,
        source_mesh_shape=(2,),
        source_placements=("Shard(0)",),
        target_mesh_shape=(2,),
        target_placements=("Shard(0)",),
        source_public_url="http://source-agent",
    )
    range_spec = builder.build(checkpoint)
    assert range_spec["checkpoint"]["format"] == "dcp"
    assert range_spec["source"]["shards"]
    assert all(
        shard["uri"].startswith("http://source-agent/v1/shards/")
        for shard in range_spec["source"]["shards"]
    )
    assert plan(range_spec)["nodes"]


def test_dcp_range_provider_materializes_via_http_agent(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, source_arrays, _tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, source_arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "source.dcp"
    export_dcp(spec, checkpoint)

    probe = socket.socket()
    probe.bind(("0.0.0.0", 0))
    port = probe.getsockname()[1]
    probe.close()
    host = _local_ip()
    source_url = f"http://{host}:{port}"
    server = build_agent_server(
        listen=f"0.0.0.0:{port}",
        controller_token="secret",
        state_dir=tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url=source_url,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for rank in range(2):
            server.agent_app.inventory(rank)
        builder = DcpRangeSpecBuilder(
            source_world=2,
            source_mesh_shape=(2,),
            source_placements=("Shard(0)",),
            target_mesh_shape=(2,),
            target_placements=("Shard(0)",),
            source_public_url=source_url,
        )
        versions = tmp_path / "versions"
        version_dir = versions / "step-100"
        version_dir.mkdir(parents=True)
        write_marker(
            version_dir,
            VersionMarker(
                version="1",
                version_id="step-100",
                job_uid="job-1",
                step=100,
                parent_id=None,
                metadata_hash="sha256:" + "a" * 64,
                created_at="2026-10-22T00:00:00Z",
                complete=True,
            ),
        )
        target = VersionTarget(tmp_path / "target")
        provider = DirectIncrementalApplyFileProvider(
            lambda descriptor: builder.build_and_plan(checkpoint),
            target=target,
            source_token="secret",
        )
        follower = CheckpointFollower(
            versions,
            target,
            chunking=ChunkingPolicy.file(),
            manifest_provider=provider,
        )
        follower.sync_once()
        for rank in range(2):
            expected = (
                tmp_path
                / "work"
                / "target"
                / "w"
                / f"rank{rank}.bin"
            ).read_bytes()
            actual = (
                target.shadow
                / "step-100"
                / "w"
                / f"rank{rank}.bin"
            ).read_bytes()
            assert actual == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
