# shardferry

[![CI](https://github.com/bolubo/shardferry/actions/workflows/ci.yml/badge.svg)](https://github.com/bolubo/shardferry/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bolubo/shardferry?include_prereleases)](https://github.com/bolubo/shardferry/releases)

**Checkpoint readiness, recovery, and mobility control plane for training
states.**

shardferry answers a control-plane question: is the latest checkpoint
complete, fresh, loadable, and movable? Restore, migration and live rank
split are actions it performs only after readiness is established.

```text
checkpoint storage
        -> CheckpointRecord
        -> readiness (complete / fresh / loadable / movable)
        -> RescueDecision
        -> RestoreDrill or CheckpointMigration
        -> proof
        -> updated readiness
```

Status: **0.2.0-rc.1 candidate**. The control-plane slice and CPU data paths
are usable locally. Kind, multi-node and GPU acceptance remain gates before a
stable release. The last published release is **0.1.1**.

## Why

Training jobs span local disks, clusters and failure domains. “The checkpoint
files exist” is not enough: an operator needs to know whether a checkpoint is
complete, fresh, actually loadable, and movable to a different world size.
shardferry keeps that state authoritative and records the proof behind it.

The project does not compete on same-host raw copy speed. Its value is
readiness semantics, cross-domain movement, resumability, proof, and
failure-safe commit.

## Quick start

```bash
python -m pip install -e ".[dev]"

shardferry demo
shardferry demo --dcp
shardferry readiness --checkpoint /data/job/source.dcp --target-world 3
shardferry restore-drill \
  --checkpoint /data/job/target-3rank.dcp \
  --target-command "python train_resume.py --checkpoint {checkpoint}"
shardferry migrate \
  --checkpoint /data/job/source.dcp \
  --target-world 3 \
  --output /data/job/target-3rank.dcp
shardferry-target-load \
  --checkpoint /data/job/target-3rank.dcp \
  --rank 0 \
  --json
shardferry-support-bundle --namespace shardferry-control
```

`demo` is dependency-free. `demo --dcp` additionally creates a real
source/target DCP pair and verifies a target DCP load.

`restore-drill` requires the target command to load model/optimizer state and
run real steps. `readiness` is read-only; a complete checkpoint stays
`UNVERIFIED` until a drill or target load proves it is loadable.

Kubernetes flow:

```bash
go build -o bin/shardferryctl ./cmd/ctl
export PATH="$PWD/bin:$PATH"
kubectl apply -f examples/checkpoint-control/checkpoint-record-selector.yaml
shardferryctl readiness training-step-100
kubectl apply -f examples/checkpoint-control/restore-drill.yaml
kubectl get sfdrill training-step-100 -w
shardferryctl readiness training-step-100
```

See [`docs/QUICKSTART.zh-CN.md`](docs/QUICKSTART.zh-CN.md) and
[`docs/USAGE.zh-CN.md`](docs/USAGE.zh-CN.md) for the full workflow.

## Control plane

| Resource | Purpose |
|---|---|
| `CheckpointRecord` | read-only checkpoint/rank/shard inventory and readiness |
| `RestoreDrill` | real target load plus N training steps, or observe an existing Job |
| `RescueDecision` | advisory `restore / restart / migrate / hold` after an event |
| `CheckpointMigration` | static/live execution path |

Drill proof is bound to checkpoint version and metadata digest. A changed
checkpoint invalidates the old `loadable=true`; metadata presence is never
treated as load proof.

## Integrations

### Kueue

The optional `kueueAdapter` reads `CheckpointRecord` readiness first and
publishes Kueue `PreemptionConfig` labels. It never replaces Kueue scheduling.

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true

shardferryctl kueue status
shardferryctl kueue explain <workload>
```

The ClusterQueue must reference the PreemptionConfig:

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

See [`docs/KUEUE-PREEMPTION-GUARD.zh-CN.md`](docs/KUEUE-PREEMPTION-GUARD.zh-CN.md).

### LRS

The optional `shardferry-live-worker` implements the Go live path's
`POST /v1/live/reconcile` contract with durable tick state and CPU-contract
proof.

```bash
shardferry-live-worker \
  --listen 0.0.0.0:9460 \
  --state-dir /var/lib/shardferry/live-worker
```

The current backend is CPU-contract; GPU collective continuity and 4-GPU/2-node
acceptance remain pending. See [`docs/LRS-WORKER.zh-CN.md`](docs/LRS-WORKER.zh-CN.md).

## Install

The published `0.1.1` wheel and images remain available:

```bash
python -m pip install shardferry-0.1.1-py3-none-any.whl
docker pull ghcr.io/bolubo/shardferry-controller:0.1.1
docker pull ghcr.io/bolubo/shardferry-agent:0.1.1
docker pull ghcr.io/bolubo/shardferry-gateway:0.1.1
```

The `0.2.0-rc.1` candidate is source-only until the GPU/cross-machine gates
pass. Helm installation is documented in
[`deploy/helm/shardferry/README.zh-CN.md`](deploy/helm/shardferry/README.zh-CN.md).

## Safety and limits

1. Source checkpoint trees are read-only.
2. Target bytes are verified before commit.
3. A failed rank cannot produce a successful overall status.
4. Retry/resume never silently re-transfers committed ranks.
5. `protected` Kueue labels are only a preemption-candidate filter, not a
   replacement for Kueue's scheduling or a guarantee against classical
   preemption.

Current limits:

- DCP is the supported checkpoint format.
- Tested topology changes cover documented world-size and DeviceMesh
  resharding; arbitrary TP/PP movement is not promised.
- GPU validation covers a 4-GPU node; physical 128-GPU and cross-host GPU
  transfer remain pending.
- The LRS worker is CPU-contract until the GPU backend is validated.

## Repository layout

```text
src/shardferry/       Python CLI, DCP adapter, agents, readiness, state, LRS
cmd/                  Go controller, ctl, gateway, kueue-adapter
internal/             control-plane models, reconcilers, providers, execution
api/ + config/crd/    CRD types and generated definitions
spec/                 cross-language v1 contracts
deploy/               Dockerfiles and Helm chart
examples/             user-facing manifests
docs/                 current product, user, operator and integration docs
```

## Documentation and development

- [Documentation index](docs/INDEX.zh-CN.md)
- [Product form](docs/PRODUCT-FORM.zh-CN.md)
- [Architecture](docs/ARCHITECTURE.zh-CN.md)
- [Roadmap](docs/ROADMAP.zh-CN.md)
- [Release notes](docs/RELEASE-NOTES-0.2.0-rc.1.md)
- [Checkpoint control-plane example](examples/checkpoint-control/README.zh-CN.md)

```bash
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/shardferry
python scripts/check_release.py --json
```

Historical design and research notes are intentionally kept outside the public
repository; public docs contain only current product, usage, architecture and
release material.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
