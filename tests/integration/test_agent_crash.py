from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from shardferry.agent import AgentApp
from shardferry.agent.executor import LocalArtifactResolver, TargetTaskExecutor
from shardferry.agent.state import TaskPhase, TaskStore
from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.verification import verify
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


@pytest.mark.skipif(os.name == "nt", reason="kill -9 harness is POSIX-only")
def test_agent_kill_during_commit_recovers_from_journal(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    plan_path.write_text(json.dumps(plan_payload), encoding="utf-8")

    target_root = tmp_path / "crash-target"
    apply(
        plan_payload,
        spec,
        target_rank=1,
        target_root=target_root,
        commit=False,
    )
    state = tmp_path / "crash-state"
    store = TaskStore(state)
    task = store.create(
        {
            "version": "1",
            "migration_uid": "m-crash",
            "commit_token": "epoch-crash",
            "rank": 1,
            "task_hash": "sha256:" + "c" * 64,
            "target_root": str(target_root),
            "spec_ref": "spec.yaml",
            "plan_ref": "plan.json",
            "plan_slice": {"nodes": [], "bytes": 0},
            "state": "created",
        }
    )
    for phase in (
        TaskPhase.PREPARING,
        TaskPhase.PREPARED,
        TaskPhase.TRANSFERRING,
        TaskPhase.STAGED,
        TaskPhase.COMMIT_READY,
        TaskPhase.COMMITTING,
    ):
        store.transition(task.task_id, phase)

    worker = Path(__file__).resolve().parents[1] / "fixtures" / "agent_crash_worker.py"
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            "--state-dir",
            str(state),
            "--artifact-root",
            str(tmp_path),
            "--kill-on-replace",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == -9, result.stderr

    executor = TargetTaskExecutor(LocalArtifactResolver(tmp_path))
    app = AgentApp("secret", state, committer=executor.commit)
    app.recover_tasks()

    assert TaskStore(state).get(task.task_id).state is TaskPhase.COMMITTED
    verify(spec, plan_payload, target_rank=1, target_root=target_root)
