# shardferry

Move and reshard local checkpoint shards across DeviceMeshes and clusters.

Status: **active development**. The product path is local `demo`, `doctor`, and
`migrate`, plus selector-based `shardferryctl migrate -f FILE --wait` for
Kubernetes local-disk agents. Python capability tests, the Go execution/
controller path, and Helm packaging are green; physical multi-node/GPU
acceptance remains environment work.

## Product path

```bash
shardferry demo
shardferry doctor --checkpoint /path/to/source.dcp
shardferry migrate --checkpoint /path/to/source.dcp --target-world 3
```

The product migration path reads tensor byte ranges directly from source
`.distcp` files; it does not extract a second raw copy into the work directory.

For distributed local disks, see `docs/USAGE.zh-CN.md` and use
`examples/k8s/migration.yaml` with `shardferryctl migrate -f ... --dry-run` or
`--wait`. Before creating the CR, run `shardferryctl preflight -f ...` to
discover the actual source/target agents and ranks.

## What it will do

`shardferry` takes local per-rank checkpoint shards from a source
cluster/DeviceMesh and rebuilds them for a target cluster/DeviceMesh, including
different world sizes, placements, and partial mesh overlap, without requiring a
global remote checkpoint.

## License

Apache-2.0. See [`LICENSE`](LICENSE).

Contributions use the workflow in [`CONTRIBUTING.md`](CONTRIBUTING.md).
Report security-sensitive issues through GitHub's private vulnerability
reporting; see [`SECURITY.md`](SECURITY.md).

## Physical A/B quick path

The local demo and the cross-machine test are intentionally separate:

```text
A (source):  dcp-inspect -> serve --public
B (target):  fetch-spec -> plan -> apply -> verify
```

A/B normally coordinate only on the A machine's reachable IP/port and when its
server is running. No shared filesystem or manual source-URI editing is needed.
The target Agent task card is
[`docs/AGENT-TARGET-PROMPT.zh-CN.md`](docs/AGENT-TARGET-PROMPT.zh-CN.md).

For many-node local-disk checkpoints, start `serve --rank N` on each source
rank, put the offsets in `source.mesh.rank_map`, and run `apply --target-rank N
--target-root /local/path` on each target rank. The 4/3 commands are the smoke
path; the rank-scoped path is the scale path.

CPU-only rank simulation:

```bash
python scripts/rank_smoke.py \
  --source-world 128 --target-world 64 \
  --work-dir /tmp/shardferry-rank-smoke
```

This starts 128 local source agents, applies 64 target ranks through HTTP, and
bitwise-verifies the result. It is a data-plane simulation, not a real
multi-node test.

Kubernetes local-disk templates and rank-map rendering are under `deploy/k8s/`
and `scripts/render_rank_map.py`. Per-rank reports can be combined with
`shardferry summarize --reports-dir DIR --out summary.json`. For a coordinator
gate, also pass `--spec client.yaml --plan plan.json`; the command then fails
unless every target rank is present and its status, verification, chunk count,
and planned bytes match the plan.

## Current contents

- `spec/`: frozen charter, verification plan, and JSON schemas.
- `spec/v1/`: V1 error, capabilities, inventory, and task contracts.
- `src/shardferry/`: CLI and implementation.
- `src/shardferry/{contracts,doctor}.py`: contract validation and read-only
  preflight checks.
- `src/shardferry/adaptive.py`: opt-in token bucket / 429 backpressure model.
- `src/shardferry/{agent,agent_state}.py`: V1 agent HTTP surface, durable task
  state, and idempotent task transitions.
- `src/shardferry/agent_executor.py`: local artifact resolution plus target
  stage/commit wiring for one rank.
- `internal/agentclient/`: Go HTTP client for inventory, planning, and tasks.
- `internal/artifactstore/`: content-addressed spec/plan artifact store and HTTP handler.
- `internal/controller/orchestrator.go`: Go inventory collection, MigrationSpec
  assembly, and Python planner invocation.
- `api/v1/`, `internal/controller/`, `cmd/controller/`: Go controller-runtime
  API, generated CRD/deepcopy, commit coordinator, and manager.
- `cmd/ctl/`: `shardferryctl migrate -f ... --wait`, get/describe, cancel, retry.
- `internal/controller/resolver.go`: selector-based agent and local-rank discovery.
- `examples/k8s/migration.yaml`: minimal selector-based 128 -> 64 migration.
- `deploy/docker/`: controller and CPU-DCP agent image builds.
- `config/crd/bases/`: controller-gen generated `CheckpointMigration` CRD.
- `src/shardferry/{mesh,manifest,inspect}.py`: Phase 1 manifest and toy
  inspection path.
- `src/shardferry/{layout,planner}.py`: mesh geometry, deterministic transfer
  plan, and strict plan validation.
- `src/shardferry/{executor,verify,tensor_io,targets}.py`: local staging,
  journal/resume, byte-range transfers, and bitwise verification.
- `src/shardferry/storage/file.py`: `file://` range storage.
- `src/shardferry/{serve,storage/http,storage/router}.py`: source HTTP agent,
  HTTP range client, and transport routing.
- `src/shardferry/dcp.py`: real PyTorch DCP metadata/chunk adapter and DCP
  export.
- `src/shardferry/coordinator.py`: expected per-target-rank work derived from
  a validated spec and plan.
- `src/shardferry/demo.py`: self-contained 4->3 demo with no external files.
- `tests/oracle/`: independent gather-to-global/re-shard oracle.
- `tests/mutation/`: mutation harness that proves the checker catches bugs.
- `tests/fixtures/`: toy tensors and mesh fixtures.
- `examples/toy-4to3/`: example MigrationSpec.
- `scripts/snapshot.sh`: phase snapshot helper.

## Local workflow

Git is present for recovery/rollback. Keep virtualenvs, build outputs, and
benchmark data out of commits; use an explicit branch/worktree for parallel
development. Phase evidence is kept outside the public checkout.

V1 contract and preflight checks:

```bash
python -m shardferry.cli capabilities --json
python -m shardferry.cli doctor --spec SPEC.yaml --json
```

`capabilities` declares the stable machine-readable surface. `doctor` is
read-only: it validates the spec, V1 schemas, source URI reachability, the
target parent/root, and a conservative source/work/output disk estimate without
creating a target directory.

Agent API foundation (HTTPS/WSS deployment is not wired yet):

```text
GET  /v1/healthz
GET  /v1/readyz
GET  /v1/capabilities
GET  /v1/inventory?rank=N
POST /v1/plan
POST /v1/tasks
POST /v1/tasks/{id}/prepare
POST /v1/tasks/{id}/execute
POST /v1/tasks/{id}/commit
POST /v1/tasks/{id}/cancel
GET  /v1/tasks/{id}
```

The target task state is persisted under the agent state directory with atomic
file replacement. Task creation is idempotent by migration/rank/task hash.
The target executor resolves `specRef`/`planRef` artifacts and calls the
existing `apply(..., commit=False)`; the separate commit endpoint calls
`commit_staged`, so staging cannot become a final target before the controller
commit barrier.
Persistent artifact storage, mTLS, and DCP chunk-cache inventory are next slices.

## V1 controller (Go)

The production controller is Go, not Python. The Python `agent` and
`agent_executor` remain the PyTorch/DCP data plane.

```bash
go test ./...
go vet ./...
go build ./cmd/controller
```

The controller requires `SHARDFERRY_AGENT_TOKEN` in its environment. It now
collects source inventories, builds a MigrationSpec, calls the Python
`/v1/plan` endpoint, and records `PlanReady` plus the plan hash in status.
It publishes spec/plan artifacts, creates target tasks, stages/commits them, and
only reports success after every target rank is committed. The current artifact
store is an in-memory HTTP store; PVC/S3 persistence and mTLS are next.

The Go module contains:

- `api/v1`: `CheckpointMigration` API types and generated deepcopy;
- `internal/controller`: 128-rank commit coordinator and reconciler validation;
- `internal/execution`: workqueue-driven per-rank execution and CommitIntent barrier;
- `internal/agentclient`: inventory/plan/task API client;
- `cmd/controller`: controller-runtime manager with leader election and probes;
- `config/crd/bases`: controller-gen CRD.

The Python side exposes the target worker with:

```bash
SHARDFERRY_AGENT_TOKEN=secret python -m shardferry.cli agent \
  --listen 0.0.0.0:9443 \
  --state-dir /var/lib/shardferry \
  --artifact-base-url http://shardferry-controller:8082
```

## Advanced CLI reference

The installed console script and `python -m shardferry` are equivalent:

```bash
shardferry migrate -c /path/to/source.dcp -n 3 -o /path/to/target.dcp
```

The common 1D FSDP case needs no topology flags:

- source world size: inferred from the DCP metadata
- source/target placement: `Shard(0)`
- target world size: source world unless `--target-world` is given
- work directory: `<checkpoint>.shardferry`
- migrated DCP output: `<checkpoint>-migrated`

For a 2D mesh, provide the topology explicitly:

```bash
shardferry migrate \
  --checkpoint /path/to/source.dcp \
  --source-mesh-shape 2,2 --source-placements "Shard(0),Shard(1)" \
  --target-mesh-shape 3,1 --target-placements "Shard(0),Replicate()" \
  --output /path/to/target.dcp
```

`--target-mesh-shape` is enough to infer the target world size. If both
`--target-world` and `--target-mesh-shape` are supplied, their product must
match.

### Lower-level workflow

```bash
python -m shardferry.cli inspect \
  --input examples/toy-4to3/checkpoint.yaml \
  --out /tmp/shardferry-migration.yaml
```

After materializing the source shard files referenced by a spec:

```bash
python -m shardferry.cli plan --spec spec.yaml --out plan.json
python -m shardferry.cli apply --spec spec.yaml --plan plan.json \
  --report report.json
python -m shardferry.cli verify --spec spec.yaml --plan plan.json
```

Target files are written under `<target>/<tensor>/rankN.bin`. `serve` remains a
source-side command:

```bash
python -m shardferry.cli serve --manifest spec.yaml --public
```

Source shard URIs must be reachable HTTP URLs on the target side. A physical
two-machine run is still required for the full MVP gate.

On the target side, `fetch-spec --server http://SOURCE:9000 --target-storage
./target --out client.yaml` pulls a portable spec from the source agent. If the
spec was copied manually, `spec-http --base-url http://SOURCE:9000
--target-storage ./target` performs the same rewrite offline.

See [`docs/QUICKSTART.zh-CN.md`](docs/QUICKSTART.zh-CN.md) for a novice-oriented
Windows/Linux walkthrough and [`docs/HANDOFF.zh-CN.md`](docs/HANDOFF.zh-CN.md)
for the classmate acceptance handoff.
For the physical A/B protocol, see
[`docs/AB-EXPERIMENT.zh-CN.md`](docs/AB-EXPERIMENT.zh-CN.md).
For the GPU/FSDP matrix and CPU gate, see
[`docs/GPU-EXPERIMENT.zh-CN.md`](docs/GPU-EXPERIMENT.zh-CN.md).
For the transfer-volume baseline, see
[`docs/BENCHMARK.zh-CN.md`](docs/BENCHMARK.zh-CN.md).
