from __future__ import annotations

import json
import socket
import threading
from urllib.request import Request, urlopen

import pytest
import yaml

from aevrel.agent import build_agent_server
from aevrel.agent.executor import LocalArtifactResolver, TargetTaskExecutor
from aevrel.planning import plan
from aevrel.verification import verify
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
    return json.loads(urlopen(request).read())


def test_target_executor_stages_then_commits_one_rank(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["checkpoint"]["format"] = "dcp"
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    executor = TargetTaskExecutor(LocalArtifactResolver(tmp_path))
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        executor=executor,
        committer=executor.commit,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target_root = tmp_path / "agent-target-rank1"
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        task = _post(
            base,
            "/v1/tasks",
            {
                "version": "1",
                "migration_uid": "m-executor",
                "commit_token": "epoch-d",
                "rank": 1,
                "task_hash": "sha256:" + "d" * 64,
                "target_root": str(target_root),
                "spec_ref": "spec.yaml",
                "plan_ref": "plan.json",
                "plan_slice": {"nodes": [], "bytes": 0},
                "state": "created",
            },
        )
        assert task["commit_token"] == "epoch-d"
        task = _post(base, f"/v1/tasks/{task['task_id']}/prepare", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/execute", {})
        assert task["state"] == "staged"
        assert not (target_root / "w" / "rank1.bin").exists()
        task = _post(base, f"/v1/tasks/{task['task_id']}/commit", {})
        assert task["state"] == "committed"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    verify(
        spec,
        plan_payload,
        target_rank=1,
        target_root=target_root,
    )
    assert (target_root / ".metadata").is_file()
    assert list(target_root.glob("*.distcp"))
