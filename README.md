# shardferry

[![CI](https://github.com/bolubo/shardferry/actions/workflows/ci.yml/badge.svg)](https://github.com/bolubo/shardferry/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bolubo/shardferry?include_prereleases)](https://github.com/bolubo/shardferry/releases)
[![License](https://img.shields.io/github/license/bolubo/shardferry)](LICENSE)

**Scheduler-neutral training-state control plane for AI infrastructure.**

Kubernetes and AI schedulers see pods, GPUs, quotas, and priorities. Training
runtimes see ranks, shards, DeviceMeshes, checkpoint versions, recovery cost,
and whether a target can actually load the state. shardferry provides the
portable state contract between those two worlds.

```text
training runtime + storage
        -> checkpoint / runtime facts
        -> completeness, freshness, loadability, movability
        -> rescue decision or mobility action
        -> proof
        -> scheduler / operator / UI
```

Checkpoint readiness is the first complete vertical. Transfer is one action;
Kueue and Live Rank Split (LRS) are adapters, not alternate sources of truth.

## What it does

| Capability | Question answered | Main surface |
|---|---|---|
| Inventory | Which checkpoint/version exists, and are all ranks/shards present? | `CheckpointRecord`, `shardferry readiness` |
| Readiness | Is it complete, fresh, loadable, and movable? | `CheckpointReadiness` |
| Verification | Did a real target load and N training steps succeed? | `RestoreDrill`, `restore-drill` |
| Rescue decision | After preemption/failure, should we restore, restart, migrate, or hold? | `RescueDecision` |
| Mobility | Can A move to B, and can a different world size/mesh continue training? | `CheckpointMigration` |
| Proof | Why should an operator or scheduler trust the result? | digest, coverage, load proof, timeline |
| Integration | How do schedulers and platforms consume the facts? | Kueue adapter, CRDs, `shardferryctl` |

The project owns the state semantics and safety boundaries. It reuses DCP,
Kubernetes, storage systems, schedulers, and training frameworks instead of
reimplementing them.

## What it is not

- It is not a replacement for Kueue, Volcano, Kubeflow, or Kubernetes scheduling.
- It is not a checkpoint format or a model-training framework.
- It is not a generic Kubernetes dashboard.
- It is not a file-copy utility with no readiness or proof model.
- It does not claim arbitrary CUDA/Python process hot migration.

## Status

`0.2.0` is the current release. The local CPU path and control-plane contracts
are usable, but real Kubernetes, multi-node, and GPU validation remain
environment-specific.

- No production SLA.
- Source checkpoint trees are read-only.
- LRS is experimental until real GPU and cross-node evidence exists.
- The source of truth for current claims is the docs and release notes, not
  issue comments or old design notes.

## Product form

shardferry is not a standalone training platform. It is a control-plane layer
that fits into an existing workflow:

- **Local:** `pip install` plus the CLI/UI for demos and one-off checks.
- **Cluster:** Helm-installed controller, agents, and CRDs for platform teams.
- **Embedded:** a checkpoint hook, sidecar, or SDK inside the training runtime.
- **Scheduler:** an adapter that publishes readiness and restore facts to
  Kueue, Volcano, or another consumer.

The UI is a view over the same control-plane facts, not a separate source of
truth.

## Requirements

- Python 3.11+ for the local CLI and Python adapters;
- PyTorch only for DCP commands, installed through the `dcp` extra;
- Go 1.24+ for the controller, agents, gateway, and `shardferryctl`;
- Kubernetes and Helm only for the cluster control plane.

## Install

For development from source:

```bash
python -m pip install -e ".[dev]"
```

For the published release, install the wheel from the release assets:

```bash
python -m pip install shardferry-0.2.0-py3-none-any.whl
```

## Quick start

```bash
python -m pip install -e ".[dev]"

# Dependency-free toy migration
shardferry demo

# Real CPU DCP pair and target-load proof
shardferry demo --dcp

# Terminal control plane
shardferry tui

# Inspect a checkpoint without modifying it
shardferry readiness --checkpoint /data/job/source.dcp --target-world 3

# Static migration to a new world size
shardferry migrate \
  --checkpoint /data/job/source.dcp \
  --target-world 3 \
  --output /data/job/target-3rank.dcp

# Rank-local target load proof
shardferry-target-load \
  --checkpoint /data/job/target-3rank.dcp \
  --rank 0 \
  --json
```

`demo` works without PyTorch. `demo --dcp` additionally requires the `dcp`
extra and proves source/target DCP load.

`shardferry tui` opens the terminal control plane.

### Real target drill

```bash
shardferry restore-drill \
  --checkpoint /data/job/target-3rank.dcp \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --steps 10 \
  --report ./reports/rank0.json

shardferry readiness \
  --checkpoint /data/job/target-3rank.dcp \
  --drill-report ./reports
```

`restore-drill` requires the target command to load model/optimizer state and
run real steps. A complete set of rank reports is required before readiness
becomes `READY`.

## Kubernetes control plane

```bash
go build -o bin/shardferryctl ./cmd/ctl
export PATH="$PWD/bin:$PATH"

kubectl apply -f examples/checkpoint-control/checkpoint-record-selector.yaml
shardferryctl readiness training-step-100

kubectl apply -f examples/checkpoint-control/restore-drill.yaml
kubectl get sfdrill training-step-100 -w
shardferryctl readiness training-step-100
```

Without a kubeconfig, use:

```bash
shardferryctl migrate -f examples/k8s/migration.yaml --dry-run
```

`preflight`, `plan`, and live control-plane commands require a reachable
management cluster.

## Diagnostics

Collect a redacted support bundle from a Kubernetes deployment:

```bash
shardferry-support-bundle --namespace shardferry-control
```

The bundle never collects Secret values by default. See
[Support](docs/SUPPORT.zh-CN.md) for the operator checklist.

## Core resources

| Resource | Purpose |
|---|---|
| `CheckpointRecord` | Read-only checkpoint/rank/shard inventory and readiness |
| `RestoreDrill` | Real target load plus N training steps, or observe an existing Job |
| `RescueDecision` | Advisory `restore / restart / migrate / hold` after an event |
| `CheckpointMigration` | Static or live execution path |

Drill proof is bound to checkpoint version and metadata digest. A changed
checkpoint invalidates `loadable=true`; metadata presence is never treated as
load proof.

## Integrations

### Kueue

The optional `kueueAdapter` consumes checkpoint readiness and publishes
preemption-protection labels. It never replaces Kueue scheduling.

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true

shardferryctl kueue status
shardferryctl kueue explain <workload>
```

The ClusterQueue must reference the configured PreemptionConfig. See
[Kueue preemption guard](docs/KUEUE-PREEMPTION-GUARD.zh-CN.md).

### Live Rank Split

`shardferry-live-worker` implements the Go live path's
`POST /v1/live/reconcile` contract with durable tick state and CPU-contract
proof. GPU collective continuity and real cross-node validation are pending.
See [LRS worker](docs/LRS-WORKER.zh-CN.md) and
[Live Rank Split flow](docs/LIVE-RANK-SPLIT-USAGE.zh-CN.md).

## Repository layout

```text
src/shardferry/       Python CLI, DCP adapter, agents, readiness, state, LRS
cmd/                  Go controller, ctl, gateway, kueue-adapter
internal/             control-plane models, reconcilers, providers, execution
api/ + config/crd/    CRD types and generated definitions
spec/                 cross-language contract schemas
deploy/               Dockerfiles and Helm chart
examples/             user-facing manifests
docs/                 current product, usage, operator and integration docs
```

## Documentation

- [Documentation index](docs/INDEX.zh-CN.md)
- [Quick start](docs/QUICKSTART.zh-CN.md)
- [Product form](docs/PRODUCT-FORM.zh-CN.md)
- [Architecture](docs/ARCHITECTURE.zh-CN.md)
- [RFC guide](docs/RFC-GUIDE.zh-CN.md)
- [Construction direction](docs/CONSTRUCTION.zh-CN.md)
- [Roadmap](docs/ROADMAP.zh-CN.md)
- [Usage](docs/USAGE.zh-CN.md)
- [Support](docs/SUPPORT.zh-CN.md)
- [Helm installation](deploy/helm/shardferry/README.zh-CN.md)
- [Checkpoint control-plane example](examples/checkpoint-control/README.zh-CN.md)
- [Release notes](docs/RELEASE-NOTES-0.2.0.md)
- [Changelog](CHANGELOG.md)

## Community and governance

- [Contributing](CONTRIBUTING.md)
- [Governance](GOVERNANCE.md)
- [Maintainers](MAINTAINERS.md)
- [Contributor ladder](CONTRIBUTOR_LADDER.md)
- [Triage policy](TRIAGE.md)
- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Release process](RELEASING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Good first issues](https://github.com/bolubo/shardferry/labels/good%20first%20issue)

Use GitHub issues for bugs, usage questions, feature requests, and integration
requests. Do not post secrets or checkpoint contents.

## Development

```bash
python scripts/check_repo.py
python scripts/check_release.py
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/shardferry
helm template shardferry deploy/helm/shardferry --include-crds \
  --set liveWorker.enabled=true --set kueueAdapter.enabled=true \
  >/tmp/shardferry-all-components.yaml
python scripts/product_acceptance.py
```

## License

Apache-2.0. See [LICENSE](LICENSE).
