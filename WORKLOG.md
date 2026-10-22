# WORKLOG

## 2026-10-21 / Phase 0

- Project name: `shardferry`.
- Language: Python 3.11+ for the MVP; JSON/YAML contracts are language-neutral.
- Versioning: no Git until MVP freeze; phase tar snapshots instead.
- Code path: `/root/lines/oss/aicluster/shardferry/`.
- Snapshot path: `/root/lines/oss/aicluster/snapshots/`.

Completed:

- Directory skeleton.
- Frozen charter and verification plan.
- MigrationSpec/Plan/Report JSON schemas.
- CLI skeleton (`inspect`, `plan`, `apply`, `verify`, `serve`).
- Module skeletons.
- Independent oracle and toy fixtures.
- Mutation harness and six mutation cases.
- Snapshot helper.

Known issues:

- Business logic is intentionally unimplemented.
- Placeholder tests are expected to xfail.
- Environment quirk: `bash -lc` in this WSL setup reports `$?` as 0 for every
  command (DEBUG trap / wrapper). Do not rely on shell exit codes. Use
  `pytest`, Python `subprocess`, or explicit status files.

Next:

- Phase 1: manifest + mesh + toy `inspect`.

Gate:

- Phase 0 passed: `pytest` 13 passed, 4 xfailed; schemas validate; CLI process
  exit code 2 verified via Python subprocess.
- Latest Phase 0 snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase0-final-*.tar.gz`.

## 2026-10-21 / Phase 1

Completed:

- MigrationSpec loading with JSON Schema + semantic validation.
- DeviceMesh/placement model for `Shard(dim)`, `Replicate()`, and
  `Partial(reduce_op)`.
- Toy checkpoint inspection and `shardferry inspect --input ... [--out ...]`.
- End-to-end CLI tests, including real subprocess exit codes.
- Corrected mesh semantics: `Shard(dim)` is a tensor dimension, not a DeviceMesh
  dimension.

Gate:

- Phase 1 passed: `pytest` 36 passed, 3 xfailed.
- All three JSON schemas pass `Draft202012Validator.check_schema`.
- Generated toy spec reloads and validates: source world size 4, target 3.
- CLI process exits: missing argument `2`, missing file `2`, successful inspect
  `0`.
- Snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase1-20261021-193108.tar.gz`.

Known issues:

- Source shard coverage/overlap analysis is intentionally deferred to the
  planner; manifest validation currently checks tensor/rank/bounds structure.
- `Partial` reduction semantics and `rank_map` transport use are implemented in
  later phases.

Next:

- Phase 2: chunk-level planner, independent oracle comparison, and randomized
  property tests.

## 2026-10-21 / Phase 2

Completed:

- Deterministic chunk planner with source-to-target rectangle partitioning.
- Different world sizes, 1D/2D meshes, `Shard`/`Replicate`, zero-length shards,
  partial source coverage checks, and source `Partial` reduction groups.
- Plan nodes now use `inputs[] + op` (`copy`/`sum`/`avg`/`max`/`min`) so one
  target cell can depend on multiple source ranks without full gather.
- Strict plan validator: schema, exact per-target-rank coverage, source ranges,
  checksums, Partial group completeness, and byte accounting.
- Independent NumPy gather/re-shard oracle and plan materializer.
- 200-example Hypothesis property test for random tensor ranks/shapes, source
  and target meshes, placements, world sizes, and zero-length shards.

Gate:

- Phase 2 passed: `pytest` 57 passed, 1 xfailed (Phase 3 placeholder).
- Example 4->3 plan: 8 nodes, 128 bytes, target ranks `{0,1,2}`.
- Plan validates against `plan.schema.json` and `validate_plan`.
- All three JSON schemas pass `Draft202012Validator.check_schema`.
- Snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase2-20261021-193631.tar.gz`.

Known issues:

- Target `Partial` placement is rejected until its output semantics are defined.
- Multiple different source `Partial` reduce operations are rejected.
- `theoretical_min_bytes` currently equals the minimum transfer bytes for the
  chosen source encoding, including required Partial contributions.

Next:

- Phase 3: `file://` adapter, staging/atomic writes, resume journal,
  `plan/apply/verify` CLI, and local 4->3 demo.

## 2026-10-21 / Phase 3

Completed:

- `file://` range storage with exact-size reads, writes, truncation, stat, and
  fsync.
- Local executor for arbitrary rectangular tensor regions, including
  `copy`/`sum`/`avg`/`max`/`min` nodes.
- Source whole-shard checksum verification before any target write.
- Per-plan staging directory and atomic `os.replace` commit to
  `<target>/<tensor>/rankN.bin`.
- Durable JSON resume journal with per-node completion, byte accounting, and
  resume/retry reporting.
- `plan`, `apply`, and `verify` CLI commands; `apply --dry-run` checks source
  and plan without creating targets.
- Bitwise verifier checks target file sizes and every planned target byte
  against source reads and reductions.

Gate:

- Phase 3 passed: `pytest` 70 passed.
- Local 4->3: `plan -> apply -> verify` real CLI subprocesses all exit `0`;
  report says `8 nodes, 128 target bytes verified`.
- Injected interruption after 4/8 nodes resumes successfully; target passes
  bitwise verification and source files are unchanged.
- Corrupt source checksum and corrupt target byte are both rejected.
- All three schemas pass `Draft202012Validator.check_schema`.
- Snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase3-20261021-194008.tar.gz`.

Known issues:

- Phase 3 transport is local `file://` only; HTTP serve is Phase 4.
- Source checksums currently hash the full shard file before applying.
- Target `Partial` placement remains unsupported; source `Partial` works.

Next:

- Phase 4: source HTTP agent, `http://` adapter, A/B two-machine run, byte
  counters, and network/kill failure injection.

## 2026-10-21 / Phase 4

Completed:

- `shardferry serve` source agent with `/health`, `/metrics`, and
  `/shard/<tensor>/<rank>` byte-range endpoints.
- SHA-256 shard metadata in HEAD responses, avoiding a full source read just to
  verify a checksum.
- `HttpStorage` and `StorageRouter`; executor/verifier now use the same logic
  for `file://` and `http(s)://` source shards.
- Source byte and request counters, plus an in-process and a real CLI-subprocess
  HTTP test.
- Injected network cut after 3 range reads resumes from the local journal and
  completes with bitwise verification.

Gate:

- Phase 4 local gate passed: `pytest` 74 passed.
- Plan-driven HTTP ranges serve exactly `total_bytes` for the toy 4->3 case.
- Real `shardferry serve` subprocess + HTTP client completes apply and verify.
- Source unavailable and mid-transfer network cut fail clearly; resume succeeds.
- All three schemas pass `Draft202012Validator.check_schema`.
- Snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase4-20261021-194634.tar.gz`.

Not yet verified:

- Physical two-machine A/B run with separate disks and no shared filesystem.
  Current evidence is two independent local processes/storage roots only.

Known issues:

- This WSL environment has broken `127.0.0.1` loopback; tests use the routable
  WSL interface address. Normal deployments should use the host IP.
- HTTP agent is plain HTTP without server-side auth/TLS; token support exists
  only in the client adapter.
- CPU PyTorch is installed in the local venv (`2.14.1+cpu`); CUDA is not
  available. A minimal DCP `save`/`load(no_dist=True)` smoke test passed, and
  a single-process `gloo` process group initialized successfully.
- DCP is declared as the optional `dcp` extra; the adapter was implemented in
  Phase 5-6.

Next:

- Phase 5: real PyTorch DCP checkpoint adapter, model/optimizer state, and
  target load/output verification.

## 2026-10-21 / Phase 5-6 local

Completed:

- Real PyTorch DCP source adapter: reads `.metadata` + byte-ranged `.distcp`
  chunks without constructing a global tensor; exports target `.distcp` +
  metadata.
- `dcp-inspect`, `dcp-export`, and one-command `dcp-migrate`; source/target
  world sizes, 1D/2D mesh shapes, placements, and target DCP output.
- Defaults: inferred source world, `Shard(0)` source/target, sibling
  `<input>.shardferry` work directory, `<input>-migrated` output.
- Self-contained `demo` and Windows/Linux wrappers; no external files needed.
- Report now includes source/target meshes, tensors, chunk counts, plan id,
  bytes, duration, retries, and bitwise verification.
- Real 4-rank DTensor DCP -> 3-rank target load test, including multiple
  tensors (`w`, bias, optimizer-like `exp_avg`) and model forward output check.
- `spec-http` rewrites a local spec into a client spec for a source-side HTTP
  agent; the target storage path can be overridden for the target machine.
- Target `Partial(sum)` uses one owner shard plus zero peers; target
  `Partial(avg)` replicates the full global shard to each peer. Both are
  covered by integration tests.
- Cross-version evidence: torch `2.13.0+cpu` source DCP -> `2.14.1+cpu`
  migration -> `2.13.0+cpu` 3-rank target worker loads successfully with the
  model-output check.
- Target `Partial(sum)`/`Partial(avg)` paths and DCP export are covered.
- GPU-ready harness scripts for FSDP source save and target load are present,
  with a CPU gate and an explicit GPU experiment matrix.
- Rank-scoped data plane is now available: `serve --rank`, `rank_map` endpoint
  routing, `apply --target-rank`, `verify --target-rank`, and target-root
  overrides. A 128-source-rank plan and a 4-agent HTTP test cover the new path.
- HTTP source storage retries transient connection failures with bounded
  exponential backoff; 404/416 responses are not retried.
- CPU rank smoke passed with 128 local source agents -> 64 target ranks and
  -> 256 target ranks, both bitwise.
- Independent Windows v2 rerun passed with the 205859 package, twice plus
  schema-checked acceptance. The follow-up plan-id canonicalization now keeps
  the identity stable across target paths and rank endpoints.
- Legacy plan IDs from `shardferry-plan-v1/v2` are accepted for validation so
  an interrupted journal from the prior package can be resumed; newly generated
  plans use the canonical ID.
- `rank_smoke.py` now chooses a reachable local host by HTTP health and supports
  an explicit `--host`, with `127.0.0.1` fallback.
- Added Kubernetes local-disk StatefulSet/Service skeletons and a rank-map
  renderer for the 128-node topology.
- Added `shardferry summarize` and a report-set aggregator for per-target-rank
  apply reports.
- Added a transfer-volume benchmark comparing plan bytes with the full-gather
  baseline. 1GB 8->5 benchmark: 1GiB plan bytes vs 2GiB baseline, 2x saving
  at the transfer-volume level.
- Source HTTP client now bypasses environment proxies by default; this matches
  direct local-cluster transport and avoids `NO_PROXY` misconfiguration.
- Target commit errors now include the exact final path and underlying OSError.
- Target path conflicts are now rejected before any source reads, so a known
  bad target does not consume the whole 1GB transfer first.
- Executor and verifier now coalesce contiguous source/target runs up to 4MB,
  removing the observed 4KB-per-request behavior for full-row shards.
- Scale smoke: 1GB 8->5 and 10GB 16->7, both bitwise verified; 10GB took about
  310s and required the root filesystem because `/tmp` is a 7.9GB tmpfs.

Gate:

- Default suite: `pytest` 118 passed, 2 opt-in distributed tests skipped.
- `SHARDFERRY_RUN_DCP_DIST=1 pytest tests/integration/test_dcp_distributed.py`:
  1 passed.
- `python -m shardferry.cli demo`: passes with `8 nodes, 128 target bytes`.
- All three schemas pass `Draft202012Validator.check_schema`.
- Final local snapshot: `/root/lines/oss/aicluster/snapshots/shardferry-phase5-6-local-final17-20261021-215419.tar.gz`.

## 2026-10-21 / A/B failure runs

- Normal 4->3 Windows A/B passed: B saw `success`, `bitwise`, and source
  checksums stayed unchanged.
- Target path conflict: clear non-zero failure, no committed target, journal
  remained `in_progress`; this run also exposed the pre-transfer preflight gap,
  now fixed and tested.
- Source checksum mismatch: clear failure before any source bytes were served;
  target directory stayed empty.
- Interruption: 19102 was stopped after about 6.2MB; the first apply failed with
  a clear HTTP connection error. Staging and journal were preserved, no final
  target was committed. The resume step was intentionally not run; do not claim
  a complete physical interruption/resume pass from this run.
- Performance finding: the client requested 4KB ranges; 1GB would need roughly
  262k HTTP requests. This is now addressed by range coalescing and was not
  benchmarked to completion.
- Range coalescing is implemented and covered by unit/integration tests; the
  full 1GB HTTP run was intentionally not repeated after the owner stopped the
  test campaign.
- A test servers 19100/19101/19102 were stopped after the run; evidence
  directories were preserved.

Known limitations:

- Physical two-machine A/B with separate disks is still pending.
- L8 independent-user acceptance is pending the classmate run.
- Real FSDP cannot be exercised on this CPU-only host: PyTorch FSDP requires a
  non-CPU accelerator. DTensor DCP is covered; GPU FSDP needs a GPU host.
- DCP parsing supports tensor chunks, not arbitrary bytes/transforms.
- DCP source extraction creates raw work files, so peak disk usage is higher
  than the source checkpoint.
- Target `Partial(max/min)` and mixed source Partial reduce ops remain
  unsupported.
- HTTP agent is plain HTTP without TLS/server-side auth.

## 2026-10-21 / independent Windows feedback

Independent user ran the original Windows handoff on Windows 10/Python 3.14.
The first command failed with `EBADF` during `FileStorage.fsync`; the user
correctly isolated it to a read-only handle and verified an O_RDWR copy in a
forked copy of the project. The root-cause fix is now in `FileStorage.fsync`,
with a regression test. The original package result remains a failure; a fresh
package and a fresh rerun are required.

Next cross-machine check:

- run `demo` on Windows with the fixed package;
- run A/B toy HTTP (`serve --public` on A, `fetch-spec/plan/apply/verify` on B);
- only then add HTTP retry/backoff/auth based on observed failures.

## 2026-10-21 / coordinator completeness

Completed:

- Added `shardferry.coordinator.expected_rank_work()`: derives per-target-rank
  chunk counts, per-tensor chunk counts, and planned bytes from a validated
  spec and plan.
- `shardferry summarize --spec SPEC --plan PLAN` is now fail-closed: missing or
  duplicate ranks, unexpected ranks, non-success status, failed verification,
  and chunk/byte mismatches all exit 2.
- Kept report-only summarization for quick inspection; `--spec` and `--plan`
  must be supplied together.
- Updated the K8s notes to distinguish report collection from validation and
  documented the current target StatefulSet restart caveat.

Gate:

- Default suite: `pytest` 118 passed, 2 skipped (both distributed tests opt-in).
- `SHARDFERRY_RUN_DCP_DIST=1`: 1 passed.
- `SHARDFERRY_RUN_DCP_HTTP=1`: 1 passed.
- `python -m shardferry.cli demo`: bitwise passed, 8 nodes / 128 target bytes.
- Final local snapshot:
  `/root/lines/oss/aicluster/snapshots/shardferry-phase5-6-local-final19-20261021-215949.tar.gz`.

## 2026-10-21 / V1 contracts foundation

Completed:

- Added `spec/v1/error.schema.json` and `capabilities.schema.json`, then
  `inventory.schema.json` and `task.schema.json`.
- Added `src/shardferry/contracts.py` with stable V1 error codes and
  non-mutating validation helpers.
- Added `shardferry capabilities --json` with the V1 API version, roles, and
  feature list.
- Added read-only `shardferry doctor --spec ... --json`: spec validation,
  V1 schema validation, source URI reachability, and target parent/root
  eligibility. It never creates or modifies target paths.
- Added contract/doctor regression tests and kept all existing V0 paths green.

Gate:

- `pytest -q`: 127 passed, 2 skipped (both distributed DCP tests opt-in).

## 2026-10-21 / V1 agent foundation

Completed:

- Added `src/shardferry/agent_state.py`: durable per-task JSON records, atomic
  temp-file + `os.replace` persistence, deterministic task IDs, idempotent
  creation, and a strict task transition graph.
- Added `src/shardferry/agent.py`: `GET /v1/healthz`, `/v1/readyz`,
  `/v1/capabilities`, `/v1/inventory`, and authenticated target task
  create/prepare/execute/commit/cancel/get endpoints.
- Added bearer-token protection for task and inventory routes; health/readiness
  remain probe-friendly.
- Added source inventory projection from an existing MigrationSpec without
  reading checkpoint bytes.
- Executor is injected; the current API foundation cannot silently mark a
  target task staged unless an executor is configured.

Gate:

- `pytest -q`: 135 passed, 2 skipped.
- `compileall`: passed.

Not yet implemented:

- CRD/controller and K8s packaging;
- mTLS and NetworkPolicy;
- DCP chunk-cache inventory and target `.distcp/.metadata` writer;
- wiring the existing `apply/verify` executor into the target task.

## 2026-10-21 / V1 agent executor

Completed:

- Split the local executor into `apply(..., commit=False)` and
  `commit_staged()`; `apply()` with default `commit=True` keeps the existing
  CLI behavior, while agent `execute` only produces a verified staging state.
- Task contract now carries `spec_ref` and `plan_ref`; full plan/spec stay in
  content-addressed artifacts.
- Added `LocalArtifactResolver` with root-escape protection and
  `TargetTaskExecutor`, which calls `apply(commit=False)` and separate
  `commit_staged()`.
- Added an end-to-end agent test: prepare → execute → staged → commit →
  target rank verification.

Gate:

- `pytest -q`: 137 passed, 2 skipped.

## 2026-10-21 / V1 Go controller foundation

Completed:

- Replaced the Python controller with a Go controller-runtime module:
  `github.com/shardferry/shardferry`.
- Added `api/v1` `CheckpointMigration` types, generated deepcopy, and a
  controller-gen CRD at `config/crd/bases/`.
- Ported the 128-rank commit coordinator to Go with missing/failed/
  commit-incomplete tests.
- Added a fake-client reconciler that validates the CR, writes `Accepted`
  conditions, and sets `pending` or `failed` status.
- Added `cmd/controller` with leader election, metrics, health, and readiness
  probes.
- Removed the Python controller implementation and its tests; Python remains
  the PyTorch/DCP agent runtime.

Gate:

- `go test ./...`: pass.
- `go vet ./...`: pass.
- `go build ./cmd/controller`: pass.
- `pytest -q`: 138 passed, 2 skipped.

Not yet implemented:

- Go inventory/plan orchestration and agent dispatch;
- artifact store implementation;
- envtest/kind CRD e2e;
- mTLS and DCP chunk-cache inventory.

## 2026-10-21 / V1 Go inventory and planning

Completed:

- Python agent inventory now includes tensor schemas; added authenticated
  `POST /v1/plan`, which invokes the existing Python planner.
- Added Go `internal/agentclient` for inventory and planning with bearer-token
  auth and V1 error-envelope propagation.
- Added Go `Orchestrator`: sorts source ranks, collects inventories, checks
  tensor/metadata consistency, assembles MigrationSpec, calls the planner, and
  hashes the plan.
- Reconciler now writes `PlanReady` and `status.planHash`; repeated reconciles
  at the same generation do not replan.
- Controller manager requires `SHARDFERRY_AGENT_TOKEN` for agent communication.

Gate:

- `pytest -q`: 139 passed, 2 skipped.
- `go test ./...`: pass.

## 2026-10-21 / V1 Go dispatch and artifact transport

Completed:

- Added content-addressed Go artifact store and HTTP handler for spec/plan
  artifacts; `ETag` and immutable caching are set.
- Added Go agent task client: create/prepare/execute/commit/cancel/get.
- Added Python `HttpArtifactResolver` with bearer auth, base-root enforcement,
  direct proxy bypass, and JSON-object validation.
- Extended Go orchestrator to publish spec/plan artifacts, create one task per
  target rank, prepare/execute all ranks, check staged states, and commit only
  after the full barrier.
- Added controller dispatch tests for all-committed and one-rank-failed cases.
- Added `shardferry agent` CLI entry for Python target workers.

Gate:

- `pytest -q`: 141 passed, 2 skipped.
- `go test -count=1 ./...`: pass.
- `go vet ./...` and `go build ./cmd/controller`: pass.

## 2026-10-22 / accidental workspace deletion and recovery

- At 08:59 local time a cleanup script executed `shutil.rmtree('.')` from the
  repository cwd; `shardferry/` was emptied.
- Restored from `snapshots/shardferry-v1-go-dispatch-20261021-235900.tar.gz`
  and replayed successful `apply_patch` operations recorded in the Codex
  session logs, with UNC-path normalization and duplicate-subparser cleanup.
- Python/CDP/cutover core is back and passes `234 passed / 17 skipped` in a
  fresh `.venv`; native release builds. The unverified skips are PyTorch-only
  DCP tests and distributed opt-in tests.
- Git was initialized after the incident. Do not use `shutil.rmtree` on
  computed or empty paths; benchmark cleanup must validate an absolute
  directory under a dedicated temp root.
- Go/K8s source recovery is not complete: several files created after the
  available snapshot are still missing or partial, so `go test ./...` is not
  a valid green gate yet. Do not overwrite the controller branch's state.

## 2026-10-22 / target-ready RTO probe

- Added `shardferry.target_probe`: waits for target `current`, checks the
  manifest root, CAS chunks and shadow files, and fails closed on mismatch.
- `CutoverService.status()` now exposes `last_cutover`, including
  `paused_seconds` and `target_ready_seconds`.
- Integration tests run the probe as a real target-ready subprocess and verify
  rollback when the probe rejects the target.
- Real `cutover-serve` HTTP smoke: promoted step-100 with
  `paused_seconds=0.0368`, `target_ready_seconds=0.0362`.
- Benchmark target probe now defaults on: 128MiB x3 8→4 passed with wall
  2.68s, RSS 94.3MiB, target-ready 0.035s; 1GiB x3 128→64 passed with wall
  21.92s, RSS 73.9MiB, target-ready 0.062s. 1GiB 16→8 reached RSS 237.8MiB
  and correctly failed the P1 150MiB gate.
- Last cutover RTO evidence is atomically persisted at
  `<target>/.shardferry/last-cutover.json`, so a CutoverService restart keeps
  `paused_seconds` / `target_ready_seconds` visible in `/v1/status`.
- Added `HttpTargetReady` and `cutover-serve --target-ready-url` for target
  job readiness endpoints. 2xx succeeds; 404/409/425/429/5xx retry until the
  deadline; other 4xx fail closed. CLI smoke promoted with one readiness call.
- Gate: `238 passed / 19 skipped` (PyTorch/native optional skips), compileall
  and `cutover-serve --help` pass. This work is on branch `python-rto` in
  worktree `shardferry-rto`; do not merge into the shared checkout while the
  parallel Go/K8s session is active.
