# shardferry V1 Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the source/target agent protocol foundation: durable task state,
idempotent transitions, health/capabilities endpoints, and target task control.

**Architecture:** A role-neutral agent HTTP server exposes the V1 API. Target tasks
are persisted under an agent state directory using atomic JSON writes and a strict
state machine. Source inventory reads from the existing MigrationSpec and range
storage without changing data-plane semantics.

**Tech Stack:** Python 3.11+, standard-library `ThreadingHTTPServer`, existing
`jsonschema` contracts, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

Status: agent state/API foundation implemented; target executor is wired through
stage/commit in the follow-up agent-executor slice. Controller, mTLS, and DCP
chunk cache remain separate slices.

## Scope Boundary

This plan implements the agent protocol foundation, not the controller and not the
DCP chunk cache. The target task executor is injected so the controller can wire
the existing `apply` path later without changing task semantics. No intermediate
Kubernetes orchestration is introduced.

## Global Constraints

- Task state values are exactly the V1 state enum.
- Repeating an idempotent request returns the existing task and does not restart work.
- Invalid transitions raise a typed `AgentStateError`, never silently succeed.
- Task persistence writes a temporary file, `fsync`s, then `os.replace`s.
- Health/readiness endpoints never mutate state.
- All new behavior has a failing test first.
- No new runtime dependency.

---

### Task 1: Durable Task State Machine

**Files:**
- Create: `src/shardferry/agent_state.py`
- Create: `tests/unit/test_agent_state.py`

**Interfaces:**
- `TaskPhase` is a `str, Enum` with the 11 V1 task states.
- `TaskRecord` is a dataclass with `task_id`, `migration_uid`, `rank`,
  `task_hash`, `target_root`, `plan_slice`, `state`, `attempts`, `error`,
  `created_at`, and `updated_at`.
- `TaskStore.create(payload: dict) -> TaskRecord` validates with
  `validate_task` and is idempotent by `(migration_uid, rank, task_hash)`.
- `TaskStore.transition(task_id: str, target: TaskPhase) -> TaskRecord` enforces
  the legal transition graph.

- [ ] **Step 1: Write the failing state tests**

```python
from shardferry.agent_state import AgentStateError, TaskPhase, TaskStore


def _task() -> dict:
    return {
        "version": "1",
        "migration_uid": "m-1",
        "rank": 0,
        "task_hash": "sha256:" + "a" * 64,
        "target_root": "/tmp/target/rank0",
        "plan_slice": {"nodes": [], "bytes": 0},
        "state": "created",
    }


def test_create_is_idempotent(tmp_path) -> None:
    store = TaskStore(tmp_path)
    first = store.create(_task())
    second = store.create(_task())
    assert first.task_id == second.task_id
    assert len(store.list()) == 1


def test_transition_happy_path(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(_task())
    for target in [
        TaskPhase.PREPARING,
        TaskPhase.PREPARED,
        TaskPhase.TRANSFERRING,
        TaskPhase.STAGED,
        TaskPhase.COMMIT_READY,
        TaskPhase.COMMITTING,
        TaskPhase.COMMITTED,
    ]:
        task = store.transition(task.task_id, target)
    assert task.state is TaskPhase.COMMITTED


def test_invalid_transition_is_rejected(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(_task())
    try:
        store.transition(task.task_id, TaskPhase.COMMITTED)
    except AgentStateError:
        pass
    else:
        raise AssertionError("invalid transition must fail")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_agent_state.py`
Expected: FAIL with `ModuleNotFoundError: shardferry.agent_state`.

- [ ] **Step 3: Implement the state machine and JSON persistence**

Create `src/shardferry/agent_state.py`. Persist each task as
`<state_dir>/tasks/<task_id>.json`; load existing files on construction. Use a
deterministic task ID:

```python
task_id = hashlib.sha256(
    f"{migration_uid}\0{rank}\0{task_hash}".encode("utf-8")
).hexdigest()
```

Allow only these transitions:

```text
created -> preparing
preparing -> prepared | failed | cancelled
prepared -> transferring | failed | cancelled
transferring -> staged | failed | cancelled
staged -> commit-ready | failed | cancelled
commit-ready -> committing | failed | cancelled
committing -> committed | commit-incomplete
commit-incomplete -> committing | failed
failed -> preparing
```

`create` must reject an existing task with the same key but a different
`target_root` or `plan_slice` payload using `AgentStateError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_agent_state.py`
Expected: PASS.

---

### Task 2: Agent Health and Target Task API

**Files:**
- Create: `src/shardferry/agent.py`
- Create: `tests/unit/test_agent.py`

**Interfaces:**
- `build_agent_server(listen: str, controller_token: str, state_dir: Path,
  executor: Callable[[TaskRecord], None] | None = None,
  spec_path: str | None = None) -> ThreadingHTTPServer`
- `GET /v1/healthz` -> `{"ok": true}`
- `GET /v1/readyz` -> `{"ok": true}`
- `GET /v1/capabilities` -> validated capabilities payload
- `POST /v1/tasks` -> created or existing `TaskRecord` as JSON
- `POST /v1/tasks/{id}/prepare`, `/execute`, `/commit`, `/cancel` -> transitioned task
- `GET /v1/tasks/{id}` -> task JSON

Authentication: requests must carry `Authorization: Bearer <controller_token>`;
missing/wrong token returns HTTP 401. Health/readiness are intentionally public for
probes.

- [ ] **Step 1: Write failing HTTP tests**

```python
import json
import threading
from urllib.request import Request, urlopen

from shardferry.agent import build_agent_server
from shardferry.agent_state import TaskStore


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


def test_agent_health_and_task_lifecycle(tmp_path) -> None:
    server = build_agent_server(
        "127.0.0.1:0", "secret", tmp_path, executor=lambda task: None
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert json.loads(urlopen(base + "/v1/healthz").read())["ok"] is True
        task = _post(
            base,
            "/v1/tasks",
            {
                "version": "1",
                "migration_uid": "m-1",
                "rank": 0,
                "task_hash": "sha256:" + "a" * 64,
                "target_root": str(tmp_path / "target"),
                "plan_slice": {"nodes": [], "bytes": 0},
                "state": "created",
            },
        )
        task = _post(base, f"/v1/tasks/{task['task_id']}/prepare", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/execute", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/commit", {})
        assert task["state"] == "committed"
        assert TaskStore(tmp_path).list()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_agent.py`
Expected: FAIL with `ModuleNotFoundError: shardferry.agent`.

- [ ] **Step 3: Implement the HTTP agent**

Use `ThreadingHTTPServer`, a handler with JSON request/response helpers, and the
same `Authorization` check for every `/v1/tasks*` route. `execute` calls the
injected executor once and transitions `transferring -> staged`; if the executor
raises, transition to `failed` and return HTTP 500 with a V1 error envelope.
`cancel` is only legal before commit and returns HTTP 409 after commit starts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_agent.py`
Expected: PASS.

---

### Task 3: Source Inventory Endpoint

**Files:**
- Modify: `src/shardferry/agent.py`
- Create: `tests/unit/test_agent_inventory.py`

**Interfaces:**
- `GET /v1/inventory?rank=<n>` returns a V1 Inventory payload.
- The endpoint reads a configured MigrationSpec and returns shard entries for the
  requested rank without reading checkpoint bytes.
- Unknown rank returns HTTP 404; malformed rank returns HTTP 400.

- [ ] **Step 1: Write the failing inventory tests**

```python
import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from shardferry.agent import build_agent_server
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def test_inventory_endpoint_returns_rank_chunks(tmp_path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    server = build_agent_server(
        "127.0.0.1:0",
        "secret",
        tmp_path / "state",
        spec_path=str(spec_path),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        request = Request(
            base + "/v1/inventory?rank=0",
            headers={"Authorization": "Bearer secret"},
        )
        payload = json.loads(urlopen(request).read())
        assert payload["rank"] == 0
        assert payload["chunks"]
        assert payload["chunks"][0]["checksum"].startswith("sha256:")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_agent_inventory.py`
Expected: FAIL because the endpoint is absent.

- [ ] **Step 3: Implement inventory projection**

Load the spec once at server construction. Map shard `offset/size/checksum` into
the V1 Inventory shape from the design; include the spec hash as `metadata_hash`
and the package version as `agent_version`. Do not open source shard files.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/unit/test_agent_inventory.py`
Expected: PASS.

---

### Task 4: Regression Gate and Snapshot

**Files:**
- Modify: `README.md`
- Modify: `WORKLOG.md`
- Modify: `tests/unit/test_agent.py`

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all tests pass; distributed DCP tests remain opt-in.

- [ ] **Step 2: Run a real agent process smoke test**

Start `python -m shardferry.cli agent ...` if the CLI entry is added, call
`/v1/healthz`, then stop the process. If the CLI entry is not part of this slice,
run the library server in a subprocess test instead.

- [ ] **Step 3: Update docs/worklog**

Document the agent API and task state values; record the exact test count.

- [ ] **Step 4: Snapshot**

Run `bash scripts/snapshot.sh v1-agents <timestamp>` after all tests pass.

