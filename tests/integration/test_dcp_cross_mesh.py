from __future__ import annotations

import hashlib
import json
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pytest

from shardferry.agent import build_agent_server
from shardferry.dcp import export_dcp, export_dcp_local, inspect_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.storage.file import FileStorage
from shardferry.verification import DcpTargetReady
from shardferry.verification import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import (
    assert_bitwise_equal,
    reference_reshard,
)


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("0.0.0.0", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _start_agent(
    tmp_path: Path,
    checkpoint: Path,
    name: str,
) -> tuple[object, threading.Thread, str]:
    port = _free_port()
    public_url = f"http://{_local_ip()}:{port}"
    server = build_agent_server(
        f"0.0.0.0:{port}",
        "secret",
        tmp_path / f"{name}-state",
        dcp_checkpoint=checkpoint,
        public_url=public_url,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, public_url


def test_real_dcp_2d_source_to_1d_3_rank_target(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "source-work")
    apply(plan(spec), spec)
    source_dcp = tmp_path / "source-2d.dcp"
    export_dcp(spec, source_dcp)
    before = _digest_tree(source_dcp)

    migrated_spec = inspect_dcp(
        source_dcp,
        tmp_path / "migrate-work",
        source_mesh_shape=(2, 2),
        source_placements=("Shard(0)", "Shard(1)"),
        target_world_size=3,
        target_mesh_shape=(3,),
        target_placements=("Shard(0)",),
    )
    migrated_plan = plan(migrated_spec)
    apply(migrated_plan, migrated_spec)
    verify(migrated_spec, migrated_plan)

    expected = reference_reshard(tensor, (3,), ("Shard(0)",))
    target_root = FileStorage().path(
        migrated_spec["target"]["storage"]["uri"]
    )
    for rank, (_, expected_array) in expected.items():
        actual = np.frombuffer(
            (target_root / "w" / f"rank{rank}.bin").read_bytes(),
            dtype=np.float32,
        ).reshape(expected_array.shape)
        assert_bitwise_equal(actual, expected_array)

    rank_zero = tmp_path / "target-rank0.dcp"
    export_dcp_local(
        migrated_spec,
        rank_zero,
        raw_root=target_root,
        ranks=(0,),
    )
    DcpTargetReady(rank_zero, rank=0)("cross-mesh")

    assert migrated_spec["source"]["mesh"] == {
        "shape": [2, 2],
        "world_size": 4,
        "placements": ["Shard(0)", "Shard(1)"],
    }
    assert migrated_spec["target"]["mesh"] == {
        "shape": [3],
        "world_size": 3,
        "placements": ["Shard(0)"],
    }
    assert _digest_tree(source_dcp) == before


def test_metadata_owner_enables_peer_local_inventory(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _tensor = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "source-work")
    apply(plan(spec), spec)
    full = tmp_path / "full.dcp"
    export_dcp(spec, full)
    before = _digest_tree(full)

    owner_root = tmp_path / "owner"
    peer_root = tmp_path / "peer"
    owner_root.mkdir()
    peer_root.mkdir()
    for path in full.iterdir():
        if path.name == ".metadata" or path.name.startswith(
            ("__0_", "__1_")
        ):
            (owner_root / path.name).write_bytes(path.read_bytes())
        elif path.name.startswith(("__2_", "__3_")):
            (peer_root / path.name).write_bytes(path.read_bytes())

    owner, owner_thread, owner_url = _start_agent(
        tmp_path,
        owner_root,
        "owner",
    )
    peer, peer_thread, peer_url = _start_agent(
        tmp_path,
        peer_root,
        "peer",
    )
    try:
        owner_metadata = urlopen(
            Request(
                owner_url + "/v1/metadata",
                headers={"Authorization": "Bearer secret"},
            )
        )
        metadata = owner_metadata.read()
        metadata_hash = owner_metadata.headers["X-Metadata-SHA256"]
        installed = urlopen(
            Request(
                peer_url + "/v1/metadata",
                data=metadata,
                method="POST",
                headers={
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/octet-stream",
                },
            )
        )
        assert json.loads(installed.read())["metadata_hash"] == metadata_hash

        inventories = {}
        for rank in range(4):
            endpoint = owner_url if rank < 2 else peer_url
            request = Request(
                endpoint + f"/v1/inventory?rank={rank}",
                headers={"Authorization": "Bearer secret"},
            )
            inventories[rank] = json.loads(urlopen(request).read())
        assert {
            inventory["metadata_hash"] for inventory in inventories.values()
        } == {metadata_hash}

        tensors = inventories[0]["tensors"]
        shards = []
        for inventory in inventories.values():
            shards.extend(
                {
                    "tensor": chunk["tensor"],
                    "rank": chunk["rank"],
                    "uri": chunk["file"],
                    "offset": chunk["tensor_offset"],
                    "size": chunk["tensor_size"],
                    "checksum": chunk["checksum"],
                }
                for chunk in inventory["chunks"]
            )
        target_root = tmp_path / "target"
        target_root.mkdir()
        migration = {
            "version": "1",
            "checkpoint": {"format": "dcp", "tensors": tensors},
            "source": {
                "mesh": {
                    "shape": [2, 2],
                    "world_size": 4,
                    "placements": ["Shard(0)", "Shard(1)"],
                },
                "storage": {"uri": "served"},
                "shards": shards,
            },
            "target": {
                "mesh": {
                    "shape": [3],
                    "world_size": 3,
                    "placements": ["Shard(0)"],
                },
                "storage": {"uri": target_root.as_uri()},
            },
        }
        translated = plan(migration)
        apply(translated, migration, token="secret")
        export_dcp_local(
            migration,
            tmp_path / "rank0.dcp",
            raw_root=target_root,
            ranks=(0,),
        )
        DcpTargetReady(tmp_path / "rank0.dcp", rank=0)("metadata-owner")
        assert _digest_tree(full) == before
    finally:
        owner.shutdown()
        owner_thread.join(timeout=5)
        owner.server_close()
        peer.shutdown()
        peer_thread.join(timeout=5)
        peer.server_close()
