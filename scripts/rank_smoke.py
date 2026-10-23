from __future__ import annotations

import argparse
import hashlib
import socket
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.remote import fetch_spec
from shardferry.serve import build_server, _public_spec
from shardferry.verification import verify


def local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def choose_host(servers, explicit: str | None) -> str:
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = []
        try:
            candidates.append(local_ip())
        except OSError:
            pass
        candidates.append("127.0.0.1")
    port = servers[0].server_address[1]
    for host in candidates:
        try:
            with urlopen(f"http://{host}:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return host
        except Exception:
            continue
    raise SystemExit(
        f"cannot reach local agent on port {port}; pass a usable --host"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-world", type=int, default=128)
    parser.add_argument("--target-world", type=int, default=64)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--host", required=False)
    args = parser.parse_args()
    root = Path(args.work_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"work directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    source_dir = root / "source"
    target_dir = root / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    global_tensor = np.arange(args.source_world * 4, dtype=np.float32).reshape(
        args.source_world, 4
    )
    shards = []
    for rank in range(args.source_world):
        array = global_tensor[rank : rank + 1].copy()
        path = source_dir / f"rank{rank}.bin"
        path.write_bytes(array.tobytes())
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": path.as_uri(),
                "offset": [rank, 0],
                "size": [1, 4],
                "checksum": "sha256:" + hashlib.sha256(array.tobytes()).hexdigest(),
            }
        )

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {"name": "w", "shape": [args.source_world, 4], "dtype": "float32"}
            ],
        },
        "source": {
            "mesh": {
                "shape": [args.source_world],
                "world_size": args.source_world,
                "placements": ["Shard(0)"],
                "rank_map": [],
            },
            "storage": {"uri": source_dir.as_uri()},
            "shards": shards,
        },
        "target": {
            "mesh": {
                "shape": [args.target_world],
                "world_size": args.target_world,
                "placements": ["Shard(0)"],
            },
            "storage": {"uri": target_dir.as_uri()},
        },
    }
    spec_path = root / "migration.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    served_spec = _public_spec(spec)

    servers = []
    threads = []
    try:
        for rank in range(args.source_world):
            server = build_server(
                str(spec_path),
                "0.0.0.0:0",
                rank=rank,
                manifest_spec=served_spec,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        host = choose_host(servers, args.host)
        for rank, server in enumerate(servers):
            served_spec["source"]["mesh"]["rank_map"].append(
                {
                    "rank": rank,
                    "endpoint": f"http://{host}:{server.server_address[1]}",
                }
            )
        client = fetch_spec(
            f"http://{host}:{servers[0].server_address[1]}",
            str(root / "target"),
            str(root / "client.yaml"),
        )
        plan_payload = plan(client)
        completed = 0
        for rank in range(args.target_world):
            target_root = root / f"target-rank{rank}"
            apply(
                plan_payload,
                client,
                target_rank=rank,
                target_root=target_root,
            )
            verify(
                client,
                plan_payload,
                target_rank=rank,
                target_root=target_root,
            )
            completed += 1
        print(
            f"source_world={args.source_world} target_world={args.target_world} "
            f"source_agents={len(servers)} target_ranks={completed} "
            f"plan_nodes={len(plan_payload['nodes'])} "
            f"bytes={plan_payload['total_bytes']} ok=True"
        )
    finally:
        for server in servers:
            server.shutdown()
        for thread in threads:
            thread.join(timeout=5)
        for server in servers:
            server.server_close()


if __name__ == "__main__":
    main()
