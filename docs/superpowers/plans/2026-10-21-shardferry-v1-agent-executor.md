# shardferry V1 Agent Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement the plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect target agent tasks to the existing apply/verify data path without
embedding a full plan in every rank task.

**Architecture:** A task carries content-addressed `spec_ref` and `plan_ref`
instead of a full plan. A local artifact resolver resolves those refs for tests
and single-cluster deployments. `TargetTaskExecutor` resolves the artifacts and
calls the existing `apply()` with the task's target rank/root.

**Tech Stack:** Python 3.11+, existing `apply`, current V1 task schema, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

Status: implemented; current default suite is 141 passed / 2 skipped.

## Global Constraints

- A task must not contain checkpoint data.
- Full plan/spec stay in artifacts; `plan_slice` is metadata for the target rank.
- Executor must call the existing validated `apply()` path, not duplicate it.
- Executor error means task state `failed`; no final success on partial write.
- No new runtime dependency.

---

### Task 1: Add spec/plan references to the task contract

**Files:**
- Modify: `spec/v1/task.schema.json`
- Modify: `src/shardferry/agent_state.py`
- Modify: `tests/unit/test_contracts.py`
- Modify: `tests/unit/test_agent_state.py`
- Modify: `tests/unit/test_agent.py`

**Interfaces:**
- `TaskRecord.spec_ref: str`
- `TaskRecord.plan_ref: str`
- `validate_task` requires both fields.

- [ ] **Step 1: Add failing contract assertions**

```python
payload = {
    "version": "1",
    "migration_uid": "m-1",
    "rank": 0,
    "task_hash": "sha256:" + "a" * 64,
    "target_root": "/tmp/target/rank0",
    "spec_ref": "spec.yaml",
    "plan_ref": "plan.json",
    "plan_slice": {"nodes": [], "bytes": 0},
    "state": "created",
}
validate_task(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py`
Expected: FAIL because `spec_ref` and `plan_ref` are rejected.

- [ ] **Step 3: Update schema, dataclass, persistence, and fixtures**

Add both required strings to `spec/v1/task.schema.json`; add them to
`TaskRecord`, `TaskStore.create`, and `_load`; update all test task payloads.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py tests/unit/test_agent_state.py tests/unit/test_agent.py`
Expected: PASS.

---

### Task 2: Artifact resolver and target executor

**Files:**
- Create: `src/shardferry/agent_executor.py`
- Create: `tests/unit/test_agent_executor.py`
- Modify: `src/shardferry/agent.py`

**Interfaces:**
- `LocalArtifactResolver(root: Path)` is callable: `resolve(ref: str) -> dict`.
- `TargetTaskExecutor(resolver)` is callable with `TaskRecord` and returns None.
- It resolves `task.spec_ref`/`task.plan_ref` and calls
  `apply(plan, spec, target_rank=task.rank, target_root=task.target_root)`.

- [ ] **Step 1: Write the failing executor integration test**

```python
from pathlib import Path

import yaml

from shardferry.agent import build_agent_server
from shardferry.agent_executor import LocalArtifactResolver, TargetTaskExecutor
from shardferry.planner import plan
from shardferry.verify import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def test_target_executor_applies_one_rank(tmp_path) -> None:
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

    executor = TargetTaskExecutor(LocalArtifactResolver(tmp_path))
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        executor=executor,
    )
    # Start server, POST a task for rank 1 with refs spec.yaml/plan.json,
    # prepare/execute/commit, then verify target rank 1 exists and is bitwise.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_agent_executor.py`
Expected: FAIL because `shardferry.agent_executor` does not exist.

- [ ] **Step 3: Implement resolver and executor**

`LocalArtifactResolver.resolve` must reject absolute paths outside its root,
load YAML/JSON using `manifest.load_payload`, and return a dict.

`TargetTaskExecutor.__call__` must call `apply` with the task rank/root and let
`ExecutionError` propagate to the agent's `failed` transition.

- [ ] **Step 4: Run test**

Run: `.venv/bin/pytest -q tests/unit/test_agent_executor.py`
Expected: PASS.

---

### Task 3: Full regression and snapshot

**Files:**
- Modify: `README.md`
- Modify: `WORKLOG.md`

- [ ] **Step 1: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: all tests pass; distributed DCP tests remain opt-in.

- [ ] **Step 2: Run compileall**

Run: `.venv/bin/python -m compileall -q src scripts tests`
Expected: no output, exit 0.

- [ ] **Step 3: Update docs/worklog and snapshot**

Document `spec_ref`/`plan_ref`, `LocalArtifactResolver`, and
`TargetTaskExecutor`; record the test count and snapshot.

