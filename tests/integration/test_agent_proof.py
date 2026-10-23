from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from shardferry.agent import build_agent_server
from shardferry.agent.executor import LocalArtifactResolver, TargetTaskExecutor
from shardferry.planning import plan
from shardferry.verification import verify_proof
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _post(base: str, path: str, payload: dict) -> dict:
    request = Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_target_agent_returns_and_merges_rank_proof(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    (tmp_path / "spec.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "plan.json").write_text(
        json.dumps(plan_payload),
        encoding="utf-8",
    )
    target_root = tmp_path / "target"
    executor = TargetTaskExecutor(
        LocalArtifactResolver(tmp_path),
        proof_enabled=True,
    )
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        executor=executor,
        committer=executor.commit,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        task = _post(
            base,
            "/v1/tasks",
            {
                "version": "1",
                "migration_uid": "proof-e2e",
                "rank": 1,
                "task_hash": "sha256:" + "a" * 64,
                "target_root": str(target_root),
                "spec_ref": "spec.yaml",
                "plan_ref": "plan.json",
                "plan_slice": {"nodes": [], "bytes": 0},
                "state": "created",
            },
        )
        task = _post(base, f"/v1/tasks/{task['task_id']}/prepare", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/execute", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/commit", {})
        report = task["report"]
        proof = report["proof"]
        assert proof["plan_hash"]
        assert proof["target_files"]

        merged = _post(
            base,
            "/v1/proof/merge",
            {"proofs": [proof]},
        )["proof"]
        assert merged["root"] == proof["root"]
        result = verify_proof(
            merged,
            target_root,
            plan=plan_payload,
        )
        assert result["passed"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
