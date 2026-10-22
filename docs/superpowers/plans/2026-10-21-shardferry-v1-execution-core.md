# shardferry V1 Execution Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the blocking target-rank loops with a workqueue-driven execution
core that owns per-rank state and a durable commit-intent barrier.

**Architecture:** `internal/execution` owns the run state machine. A typed
client-go workqueue processes one rank transition per item. `Run` records
pending/prepared/staged/committing/committed state, and the barrier sets
`CommitIntent` before any commit is dispatched. The controller will submit a
Run and return; agent polling and commit repair happen through the queue.

**Tech Stack:** Go 1.24, `k8s.io/client-go/util/workqueue`, existing
`agentclient.Task`, fake task client tests.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

## Global Constraints

- No rank is committed unless every target rank is staged.
- `CommitIntent` is set before the first commit request.
- A failed rank must not produce a succeeded summary.
- Queue keys are in-memory only; CR status/report remains the durable summary.
- No per-rank Kubernetes CRs.
- No change to the existing Orchestrator until the core tests pass.

---

### Task 1: Run and Rank State

**Files:**
- Create: `internal/execution/run.go`
- Create: `internal/execution/run_test.go`

**Interfaces:**
- `Phase`: `pending`, `prepared`, `staged`, `committing`, `committed`,
  `failed`, `commit-incomplete`.
- `NewRun(config RunConfig) *Run`
- `Run.RegisterRank(rank int32)`
- `Run.SetPhase(rank int32, phase Phase, taskID, errorText string)`
- `Run.TryBeginCommit() ([]int32, bool)`
- `Run.Summary() Summary`

- [ ] **Step 1: Write failing state tests**

```go
func TestRunNeedsEveryRankBeforeCommitIntent(t *testing.T) {
    run := NewRun(RunConfig{ID: "m", TargetWorld: 3})
    for rank := int32(0); rank < 3; rank++ { run.RegisterRank(rank) }
    run.SetPhase(0, PhaseStaged, "t0", "")
    run.SetPhase(1, PhaseStaged, "t1", "")
    if _, ok := run.TryBeginCommit(); ok { t.Fatal("commit too early") }
    run.SetPhase(2, PhaseStaged, "t2", "")
    ranks, ok := run.TryBeginCommit()
    if !ok || len(ranks) != 3 || !run.CommitIntent { t.Fatalf("bad barrier: %v %v", ranks, ok) }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/execution`
Expected: FAIL because `internal/execution` does not exist.

- [ ] **Step 3: Implement Run**

Use a mutex around all transitions. `TryBeginCommit` must be idempotent and
must reject failed/commit-incomplete ranks. `Summary` returns counts and the
commit-intent flag.

- [ ] **Step 4: Run tests**

Run: `go test ./internal/execution`
Expected: PASS.

---

### Task 2: Workqueue Runner

**Files:**
- Create: `internal/execution/runner.go`
- Create: `internal/execution/runner_test.go`

**Interfaces:**
- `TaskClient` interface with create/prepare/execute/commit.
- `NewRunner(client TaskClient, workers int) *Runner`.
- `Runner.Submit(run *Run)`.
- `Runner.Start(ctx context.Context)`.
- `Runner.Stop()`.
- `Run.Wait(ctx context.Context) error`.

- [ ] **Step 1: Write a failing 128-rank runner test**

```go
func TestRunnerCommits128Ranks(t *testing.T) {
    run := newTestRun(128)
    client := newFakeTaskClient()
    runner := NewRunner(client, 16)
    runner.Submit(run)
    ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    runner.Start(ctx)
    if err := run.Wait(ctx); err != nil { t.Fatal(err) }
    if run.Summary().Phase != PhaseSucceeded { t.Fatalf("phase=%s", run.Summary().Phase) }
    if client.commitCount != 128 { t.Fatalf("commits=%d", client.commitCount) }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/execution`
Expected: FAIL because the runner does not exist.

- [ ] **Step 3: Implement the runner**

Use `workqueue.NewTypedRateLimitingQueue` with
`DefaultTypedControllerRateLimiter`. Each queue key is `{RunID, Rank}`.
Process one phase transition per key:

```text
pending -> create/prepare -> prepared -> enqueue
prepared -> execute -> staged -> TryBeginCommit
committing -> commit -> committed
failed/commit-incomplete -> bounded retry or terminal summary
```

`Run.Wait` closes when the run is succeeded or terminally failed.

- [ ] **Step 4: Run test**

Run: `go test ./internal/execution`
Expected: PASS.

---

### Task 3: Failure Barrier

**Files:**
- Modify: `internal/execution/runner_test.go`

- [ ] **Step 1: Add a one-rank-failed test**

```go
func TestRunnerNeverCommitsWhenOneRankFails(t *testing.T) {
    run := newTestRun(3)
    client := newFakeTaskClient()
    client.failExecuteRank = 1
    runner := NewRunner(client, 3)
    runner.Submit(run)
    ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
    defer cancel()
    runner.Start(ctx)
    waitForTerminal(t, run)
    if client.commitCount != 0 { t.Fatalf("commits=%d", client.commitCount) }
    if run.Summary().Phase != PhaseFailed { t.Fatalf("phase=%s", run.Summary().Phase) }
}
```

- [ ] **Step 2: Add a commit-intent ordering test**

Assert the fake client sees `run.CommitIntent == true` on every `CommitTask`.

- [ ] **Step 3: Run tests**

Run: `go test ./internal/execution`
Expected: PASS.

---

### Task 4: Full Regression and Controller Handoff

- [ ] **Step 1: Run Go and Python regressions**
- [ ] **Step 2: Document that Reconciler must submit an execution Run instead of
  calling `Orchestrator.Dispatch` synchronously.
- [ ] **Step 3: Do not create a snapshot.**
