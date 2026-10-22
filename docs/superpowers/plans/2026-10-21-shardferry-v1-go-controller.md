# shardferry V1 Go Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement the plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the production controller in Go with controller-runtime, generated
CRD/client types, a pure commit-barrier coordinator, and a real reconcile loop.

**Architecture:** Python remains the PyTorch/DCP agent runtime. Go owns the
Kubernetes API, reconcile loop, workqueue, conditions, and leader election. The
two planes communicate through the versioned JSON contracts already defined.

**Tech Stack:** Go 1.24+, controller-runtime v0.22.5, controller-gen v0.16.1,
fake-client and envtest-style tests, Python agent contracts.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

Status: API types, generated CRD/deepcopy, coordinator, reconciler validation,
Go agent client, inventory collection, and planner invocation are implemented.
Artifact persistence and target task dispatch are next.

---

### Task 1: Generated API and CRD

**Files:**
- Create: `api/v1/groupversion_info.go`
- Create: `api/v1/checkpointmigration_types.go`
- Generate: `api/v1/zz_generated.deepcopy.go`
- Generate: `config/crd/bases/shardferry.io_checkpointmigrations.yaml`

**Interfaces:**
- Group `shardferry.io`, version `v1`, kind `CheckpointMigration`.
- Spec/status types match the final design.
- `CheckpointMigration` implements `runtime.Object` and deepcopy.

- [ ] **Step 1: Regenerate after any API type change**

Run:

```bash
controller-gen object paths=./api/...
controller-gen crd:crdVersions=v1 paths=./api/... \
  output:crd:artifacts:config=config/crd/bases
```

Expected: `api/v1/zz_generated.deepcopy.go` and the CRD YAML update.

- [ ] **Step 2: Verify CRD contract**

Run: `.venv/bin/pytest -q tests/unit/test_crd.py`
Expected: PASS.

---

### Task 2: Go Commit Barrier

**Files:**
- Create: `internal/controller/coordinator.go`
- Create: `internal/controller/coordinator_test.go`

**Interfaces:**
- `NewCoordinator(uid string, targetWorld int) *Coordinator`
- `RegisterRank(rank int) error`
- `SetRankState(rank int, state TaskPhase, message string) error`
- `CanCommit() bool`
- `BeginCommit() ([]int, error)`
- `Summary() Summary`

- [ ] **Step 1: Run the 128-rank tests**

Run: `go test ./internal/controller -run 'TestCommit|Test128|TestMissing|TestFailed' -v`
Expected: all PASS.

- [ ] **Step 2: Prove fail-closed behavior**

Tests must cover missing rank, failed rank, commit-incomplete, and 128-rank
barrier before any commit is issued.

---

### Task 3: Reconciler Validation and Status

**Files:**
- Create: `internal/controller/reconciler.go`
- Create: `internal/controller/reconciler_test.go`
- Create: `cmd/controller/main.go`

**Interfaces:**
- `Reconciler` embeds `client.Client`; `SetupWithManager` registers the controller.
- Valid spec → status phase `pending`, condition `Accepted=True`.
- Invalid spec → phase `failed`, condition `Accepted=False`.
- Manager enables leader election, health/readiness, and metrics.

- [ ] **Step 1: Run fake-client reconcile tests**

Run: `go test ./internal/controller -run TestReconciler -v`
Expected: PASS.

- [ ] **Step 2: Build the binary**

Run: `go build ./cmd/controller`
Expected: exit 0.

---

### Task 4: Inventory, Planning, and Agent Dispatch

This is the next independent slice and must not be implemented in the Python
controller:

- Go calls source agent `/v1/inventory` for each rank;
- Go validates metadata owner and builds `Inventory`;
- Go writes plan artifact through the artifact store interface;
- Go creates target tasks with `spec_ref`/`plan_ref`;
- Go drives prepare/stage and the commit barrier;
- Go aggregates reports and conditions.

### Task 5: K8s E2E

- kind + CRD + controller + Python agents;
- 128 source ranks simulated with 8 agents × 16 ranks;
- controller restart, agent restart, node drain, missing rank, and
  `CommitIncomplete` tests;
- no per-rank CRs.

