# shardferry V1 Go Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the Go controller's plan to target agents through content-addressed
artifacts and the V1 task API, with a fail-closed commit barrier.

**Architecture:** The controller publishes small spec/plan artifacts through an
artifact store HTTP endpoint. Target agents resolve those refs, stage their ranks,
and expose task state. The Go orchestrator creates/prepares/executes all target
tasks, commits only after every rank is staged, and records the aggregate result.

**Tech Stack:** Go controller, HTTP artifact server, Python agent task API,
versioned JSON contracts, `httptest`.

**Spec:** `docs/superpowers/specs/2026-10-21-shardferry-v1-production-design.md`

Status: implemented through artifact store, task client, target dispatch, and
commit barrier; persistent PVC/S3 artifact storage and envtest/kind e2e remain.

## Global Constraints

- Artifacts contain spec/plan/report only, never checkpoint data.
- A task must not contain a full plan; it carries `spec_ref` and `plan_ref`.
- Commit is impossible unless every target rank reports `staged`.
- Missing or failed ranks keep the aggregate out of `succeeded`.
- HTTP artifact refs are content-addressed by SHA256.
- No new dependency beyond the Go/Python stacks already in the workspace.

---

### Task 1: Go Artifact Store

**Files:**
- Create: `internal/artifactstore/store.go`
- Create: `internal/artifactstore/store_test.go`

**Interfaces:**
- `Store` interface: `Put(ctx, []byte) (Ref, error)` and `Get(ctx, ref string)`.
- `MemoryStore` supports an HTTP handler for `GET /v1/artifacts/{ref}`.
- `Ref` includes `URL`, `Hash`, and `Size`.

- [ ] **Step 1: Write failing tests**

```go
func TestMemoryStorePublishesContentAddressedArtifact(t *testing.T) {
    store := NewMemoryStore("http://controller:8082")
    ref, err := store.Put(context.Background(), []byte(`{"version":"1"}`))
    if err != nil { t.Fatal(err) }
    if ref.URL == "" || ref.Hash == "" { t.Fatal("missing ref") }
    request := httptest.NewRequest(http.MethodGet, ref.URL, nil)
    response := httptest.NewRecorder()
    store.Handler().ServeHTTP(response, request)
    if response.Code != http.StatusOK { t.Fatalf("status=%d", response.Code) }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/artifactstore`
Expected: FAIL because the package does not exist.

- [ ] **Step 3: Implement the store**

Hash bytes with SHA256, store immutable blobs in memory, and serve them with
`Content-Type: application/json`, `ETag`, and `Cache-Control: immutable`.
Reject path traversal and missing refs with 404.

- [ ] **Step 4: Run test to verify it passes**

Run: `go test ./internal/artifactstore`
Expected: PASS.

---

### Task 2: Go Task API Client

**Files:**
- Modify: `internal/agentclient/client.go`
- Modify: `internal/agentclient/client_test.go`

**Interfaces:**
- `Task` struct with `Version`, `MigrationUID`, `Rank`, `TaskHash`,
  `TargetRoot`, `SpecRef`, `PlanRef`, `PlanSlice`, `State`.
- `CreateTask`, `PrepareTask`, `ExecuteTask`, `CommitTask`, `CancelTask`,
  `GetTask`.

- [ ] **Step 1: Add failing HTTP client tests**

```go
func TestCreateAndAdvanceTask(t *testing.T) {
    server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        // Assert POST /v1/tasks and return a task with state=created.
    }))
    client := New("secret")
    task, err := client.CreateTask(context.Background(), server.URL, Task{Version:"1"})
    if err != nil { t.Fatal(err) }
    if task.State != "created" { t.Fatalf("state=%s", task.State) }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `go test ./internal/agentclient`
Expected: FAIL because task methods do not exist.

- [ ] **Step 3: Implement task requests**

Use the existing authenticated `do` helper. POST bodies to `/v1/tasks` and
`/v1/tasks/{id}/{prepare|execute|commit|cancel}`; GET `/v1/tasks/{id}`.
Preserve the agent's state string verbatim.

- [ ] **Step 4: Run test**

Run: `go test ./internal/agentclient`
Expected: PASS.

---

### Task 3: Python HTTP Artifact Resolver

**Files:**
- Modify: `src/shardferry/agent_executor.py`
- Create: `tests/unit/test_http_artifact_resolver.py`

**Interfaces:**
- `HttpArtifactResolver(base_url, token)` implements `.resolve(ref)`.
- It accepts content-addressed refs, sends `Authorization: Bearer`, and uses
  direct connections by default like the existing `HttpStorage`.

- [ ] **Step 1: Write the failing resolver test**

```python
def test_http_resolver_fetches_spec(tmp_path) -> None:
    # Start a local HTTP handler serving a JSON spec at /v1/artifacts/<hash>.
    resolver = HttpArtifactResolver(server_url, "secret")
    assert resolver.resolve("sha256:" + "a" * 64)["version"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/unit/test_http_artifact_resolver.py`
Expected: FAIL because `HttpArtifactResolver` does not exist.

- [ ] **Step 3: Implement resolver**

Only accept `sha256:` refs or absolute URLs under the configured base. Reject
traversal and non-JSON responses. Do not use environment proxies.

- [ ] **Step 4: Run test**

Run: `.venv/bin/pytest -q tests/unit/test_http_artifact_resolver.py`
Expected: PASS.

---

### Task 4: Go Orchestrator Dispatch and Commit Barrier

**Files:**
- Modify: `internal/controller/orchestrator.go`
- Modify: `internal/controller/orchestrator_test.go`

**Interfaces:**
- `TaskAPI` interface with create/prepare/execute/commit/get.
- `Orchestrator.Tasks`, `Orchestrator.Artifacts`.
- `Dispatch(ctx, migration, prepared) (DispatchResult, error)`.
- `DispatchResult` contains task states/reports and `Committed` boolean.

- [ ] **Step 1: Add failing tests**

```go
func TestDispatchCommitsOnlyAfterEveryRankIsStaged(t *testing.T) {
    // Fake TaskAPI returns staged for every rank; assert commit calls == 3.
}

func TestDispatchDoesNotCommitWhenOneRankFails(t *testing.T) {
    // One execute returns failed; assert zero commit calls.
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `go test ./internal/controller -run TestDispatch`
Expected: FAIL because dispatch is not implemented.

- [ ] **Step 3: Implement dispatch**

Build one task per target rank using the target endpoint/root template and the
prepared artifact refs. Prepare all, execute all, re-check all states, then
commit all. Any failed/missing rank returns before commit.

- [ ] **Step 4: Run tests**

Run: `go test ./internal/controller`
Expected: PASS.

---

### Task 5: Full Regression and Snapshot

Run Python and Go suites, `go vet`, `go build ./cmd/controller`, then update
README/WORKLOG/CARD and snapshot `v1-go-dispatch`.

