# shardferry V1 Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the machine-readable V1 contracts that controller, agents, CLI, and
future operators all share before any orchestration code is written.

**Architecture:** JSON Schema files are the source of truth. A small Python
contracts module loads, validates, and exposes contract versions. Existing V0
schemas remain intact until the V1 controller migration is complete.

**Tech Stack:** Python 3.11+, `jsonschema` 4.x, existing `pytest`, no new runtime
dependency.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

Status: implemented in the current worktree; default suite is 127 passed / 2 skipped.

## Scope Boundary

This is the first independently testable V1 sub-project. It does **not** implement
the controller, agents, CRD reconciliation, or checkpoint data path. Those get
separate plans after these contracts are green. The contract layer must be
usable by all of them and must not depend on Kubernetes.

## Global Constraints

- V1 contract version is `1`.
- CRD API group is `shardferry.io/v1`; no V0 schema may be silently changed.
- Error codes are stable identifiers, not free-form messages.
- Every contract validator returns a human-readable error and never mutates input.
- No new third-party dependency is introduced in this sub-project.
- All new public behavior is covered by TDD tests.

---

### Task 1: Error and Capability Contracts

**Files:**
- Create: `spec/v1/error.schema.json`
- Create: `spec/v1/capabilities.schema.json`
- Create: `src/shardferry/contracts.py`
- Create: `tests/unit/test_contracts.py`

**Interfaces:**
- `ERROR_CODES: frozenset[str]` contains the 18 codes from the V1 design.
- `validate_error(payload: dict) -> None`
- `validate_capabilities(payload: dict) -> None`
- `contract_version() -> str` returns `"1"`.

- [ ] **Step 1: Write the failing tests**

```python
from shardferry.contracts import ERROR_CODES, validate_capabilities, validate_error


def test_error_contract_accepts_known_code() -> None:
    validate_error(
        {
            "version": "1",
            "code": "DISK_FULL",
            "category": "target",
            "retryable": False,
            "phase": "preparing",
            "rank": 7,
            "details": "/local/checkpoints is full",
        }
    )
    assert "DISK_FULL" in ERROR_CODES


def test_capabilities_contract_accepts_agent_features() -> None:
    validate_capabilities(
        {
            "version": "1",
            "apiVersion": "shardferry.io/v1",
            "agentVersion": "1.0.0",
            "roles": ["source", "target"],
            "features": ["dcp", "range-read", "bitwise-verify"],
        }
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'shardferry.contracts'`.

- [ ] **Step 3: Write minimal schemas and module**

Create `spec/v1/error.schema.json` with required fields `version`, `code`,
`category`, `retryable`, `phase`, `rank`, `details`; `version` must be `"1"`;
`code` must be one of the 18 codes; `rank` is a non-negative integer.

Create `spec/v1/capabilities.schema.json` with required fields `version`,
`apiVersion`, `agentVersion`, `roles`, `features`; roles are `source` and/or
`target`; features are unique strings.

Create `src/shardferry/contracts.py` with the constants and validation functions.
Load schemas relative to the package root the same way `manifest.py` loads its
schema. Raise `ValueError` with the first schema error.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py`
Expected: PASS.

---

### Task 2: Inventory and Task Contracts

**Files:**
- Create: `spec/v1/inventory.schema.json`
- Create: `spec/v1/task.schema.json`
- Modify: `src/shardferry/contracts.py`
- Modify: `tests/unit/test_contracts.py`

**Interfaces:**
- `validate_inventory(payload: dict) -> None`
- `validate_task(payload: dict) -> None`
- `Inventory` requires `version`, `rank`, `format`, `metadata_hash`, `chunks`,
  `agent_version`, `torch_version`.
- `Task` requires `version`, `migration_uid`, `rank`, `task_hash`, `target_root`,
  `plan_slice`, `state`.

- [ ] **Step 1: Write failing tests**

```python
def test_inventory_contract_accepts_dcp_chunk() -> None:
    validate_inventory(
        {
            "version": "1",
            "rank": 0,
            "format": "dcp",
            "metadata_hash": "sha256:" + "a" * 64,
            "chunks": [
                {
                    "tensor": "w",
                    "rank": 0,
                    "file": "job42/__0_0.distcp",
                    "tensor_offset": [0, 0],
                    "tensor_size": [4, 4],
                    "byte_ranges": [[0, 64]],
                    "checksum": "sha256:" + "b" * 64,
                }
            ],
            "agent_version": "1.0.0",
            "torch_version": "2.14.1+cu128",
        }
    )


def test_task_contract_accepts_target_rank() -> None:
    validate_task(
        {
            "version": "1",
            "migration_uid": "m-123",
            "rank": 3,
            "task_hash": "sha256:" + "c" * 64,
            "target_root": "/local/checkpoints/rank3",
            "plan_slice": {"nodes": [], "bytes": 0},
            "state": "created",
        }
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py`
Expected: FAIL with missing `validate_inventory` or `validate_task`.

- [ ] **Step 3: Implement schemas and validators**

Add the two schemas and load them through the same schema directory helper used by
Task 1. Validate chunk offsets/sizes as non-negative integer arrays, checksums as
`sha256:` plus 64 lowercase hex characters, and task state as one of
`created`, `preparing`, `prepared`, `transferring`, `staged`, `commit-ready`,
`committing`, `committed`, `failed`, `cancelled`, `commit-incomplete`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py`
Expected: PASS.

---

### Task 3: CLI `capabilities` Surface

**Files:**
- Modify: `src/shardferry/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- `shardferry capabilities --json` prints a capabilities JSON object.
- `main(["capabilities", "--json"])` returns `0`.
- The output is validated with `validate_capabilities`.

- [ ] **Step 1: Write the failing test**

```python
def test_capabilities_cli_is_machine_readable(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == "shardferry.io/v1"
    assert "dcp" in payload["features"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_cli.py::test_capabilities_cli_is_machine_readable`
Expected: FAIL because `capabilities` is not a recognized command.

- [ ] **Step 3: Add the command**

Add a `capabilities` subcommand with a required `--json` flag. It must print only
JSON to stdout and return `0`. Include `agentVersion` from `shardferry.__version__`,
roles `["source", "target"]`, and features `["dcp", "range-read", "bitwise-verify"]`.
Validate the payload before printing.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/unit/test_cli.py::test_capabilities_cli_is_machine_readable`
Expected: PASS.

---

### Task 4: Read-only `doctor` Surface

**Files:**
- Modify: `src/shardferry/cli.py`
- Create: `src/shardferry/doctor.py`
- Create: `tests/unit/test_doctor.py`

**Interfaces:**
- `doctor(spec_path: str) -> dict`
- `doctor` returns `{"status": "ok"|"failed", "checks": [...]}`.
- It validates the spec, checks source shard existence, and checks target root
  parent existence/permissions without creating or modifying anything.

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

import yaml

from shardferry.doctor import doctor
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _materialized_spec(tmp_path: Path) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    spec["target"]["storage"]["uri"] = (tmp_path / "new-target").as_uri()
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def test_doctor_reports_ok_for_materialized_spec(tmp_path) -> None:
    result = doctor(str(_materialized_spec(tmp_path)))
    assert result["status"] == "ok"
    assert all(check["ok"] for check in result["checks"])
    assert not (tmp_path / "new-target").exists()


def test_doctor_reports_missing_source_without_writing(tmp_path) -> None:
    spec_path = _materialized_spec(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["source"]["shards"][0]["uri"] = (tmp_path / "missing.bin").as_uri()
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    result = doctor(str(spec_path))

    assert result["status"] == "failed"
    assert any(
        not check["ok"] and check["name"] == "source-shards"
        for check in result["checks"]
    )
    assert not (tmp_path / "new-target").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_doctor.py`
Expected: FAIL because `shardferry.doctor` does not exist.

- [ ] **Step 3: Implement doctor**

Reuse `load_spec`, `StorageRouter.exists`, and `targets.target_root_path`.
Checks are read-only: `spec`, `source-shards`, `target-parent`, `contracts`.
Never call `mkdir`, `write`, `replace`, or create a target file.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest -q tests/unit/test_doctor.py`
Expected: PASS.

---

### Task 5: Contract Regression Gate

**Files:**
- Modify: `tests/unit/test_contracts.py`
- Modify: `README.md`
- Modify: `WORKLOG.md`

**Interfaces:**
- All schemas pass `Draft202012Validator.check_schema`.
- Existing V0 schema tests still pass.
- README documents `capabilities` and `doctor`.

- [ ] **Step 1: Add schema regression test**

```python
def test_all_v1_schemas_are_valid() -> None:
    for path in sorted((ROOT / "spec" / "v1").glob("*.schema.json")):
        Draft202012Validator.check_schema(
            json.loads(path.read_text(encoding="utf-8"))
        )
```

- [ ] **Step 2: Run the targeted suite**

Run: `.venv/bin/pytest -q tests/unit/test_contracts.py tests/unit/test_doctor.py tests/unit/test_cli.py`
Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS with the existing opt-in DCP tests skipped by default.

- [ ] **Step 4: Update docs and worklog**

Add the two commands and the V1 contract directory to README. Add a short
WORKLOG entry with the exact test count and the new files.

