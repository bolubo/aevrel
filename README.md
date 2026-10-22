# shardferry

[![CI](https://github.com/bolubo/shardferry/actions/workflows/ci.yml/badge.svg)](https://github.com/bolubo/shardferry/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bolubo/shardferry?include_prereleases)](https://github.com/bolubo/shardferry/releases)

**Checkpoint readiness, recovery, and mobility control plane for training states.**

shardferry continuously inventories training checkpoints and answers whether
they are complete, fresh, loadable, and movable. Migration, restore drills and
live rank handoff are actions of that control plane, not the product center.

```text
local checkpoint
        -> CheckpointRecord
        -> Readiness (complete / fresh / loadable / movable)
        -> RescueDecision
        -> RestoreDrill or MobilityTransaction
        -> Proof
        -> updated readiness
```

Status: **0.2.0-rc.1 candidate**. The checkpoint/migration substrate and the
first readiness/restore/rescue control-plane slice are usable locally; kind,
multi-node and GPU acceptance are still pending. The last published release is
**0.1.1**.

Roadmap: [`0.1.x` static checkpoint mobility](docs/0.1-ACCEPTANCE.zh-CN.md) is
released. The current work is the
[`0.2.0-rc.1` checkpoint mobility candidate](docs/RELEASE-NOTES-0.2.0-rc.1.md);
GPU and cross-machine gates must pass before `0.2.0` is published. See the full
[`roadmap`](docs/ROADMAP.zh-CN.md).
Product form and the daily checkpoint-readiness contract:
[`docs/PRODUCT-FORM.zh-CN.md`](docs/PRODUCT-FORM.zh-CN.md).
Checkpoint ecosystem and user-demand research:
[`docs/CHECKPOINT-RESEARCH.zh-CN.md`](docs/CHECKPOINT-RESEARCH.zh-CN.md).
Engineering build plan and hard gates:
[`docs/BUILD-PLAN.zh-CN.md`](docs/BUILD-PLAN.zh-CN.md).
Control-plane architecture and refactor tasks:
[`docs/ARCHITECTURE.zh-CN.md`](docs/ARCHITECTURE.zh-CN.md),
[`docs/REFACTOR-PLAN.zh-CN.md`](docs/REFACTOR-PLAN.zh-CN.md).

## Why

A 128-rank training job can leave DCP shards on local NVMe, with no shared
checkpoint filesystem. The hard part is not only moving bytes; it is knowing
which checkpoint is complete, how much progress would be lost, whether a target
can actually load it, and what to do after a preemption or failure.

shardferry:

- maintains a read-only inventory of checkpoint versions, ranks and shards;
- computes readiness and target compatibility;
- records the last real restore/target-load proof;
- recommends restore, restart, migrate, or hold;
- executes migration or live handoff only after readiness is established.

### Mobility is one action

For a move, shardferry:

- discovers source and target ranks instead of requiring 128 hand-written
  mappings;
- transfers the small DCP metadata from one owner rank to peer source agents,
  so source nodes do not need a shared checkpoint directory;
- reads tensor ranges directly from `.distcp` files;
- reshards across target world sizes and DeviceMeshes;
- resumes after interrupted transfers;
- verifies target bytes and requires all ranks to commit together;
- returns a target DCP that the target training job can load.

The project does **not** try to win same-host raw-copy benchmarks. Native DCP
load-time resharding is faster when source and target already share a
filesystem. shardferry's value is readiness, cross-domain movement, resumability,
proof, and failure-safe commit.

The 0.2 benchmark target is not raw MB/s: it is `time-to-first-step`,
`steps-lost=0`, cutover delta bytes, and 90%-failure retransfer bytes across
`100Mbps / 1Gbps / 10Gbps` shaped networks.

## Install

### Python wheel (published 0.1.1)

Download the `shardferry-0.1.1-py3-none-any.whl` asset from the GitHub release:

```bash
python -m pip install shardferry-0.1.1-py3-none-any.whl
```

The `0.2.0-rc.1` candidate is source-only until the GPU gate passes:

```bash
python -m pip install -e .
```

For DCP commands, install a compatible CUDA or CPU PyTorch build in the same
environment. Python 3.11+ is required.

### Containers

```bash
docker pull ghcr.io/bolubo/shardferry-controller:0.1.1
docker pull ghcr.io/bolubo/shardferry-gateway:0.1.1
docker pull ghcr.io/bolubo/shardferry-agent:0.1.1
```

### Helm chart

The release chart is published as an OCI artifact:

```bash
helm upgrade --install shardferry \
  oci://ghcr.io/bolubo/charts/shardferry \
  --version 0.1.1 \
  -n shardferry --create-namespace
```

The default chart enables TLS secrets and expects an agent token Secret. See
[`deploy/helm/shardferry/README.zh-CN.md`](deploy/helm/shardferry/README.zh-CN.md)
for the complete token, hostPath, gateway and mTLS setup. To install the chart
from a source checkout instead, replace the OCI reference with
`deploy/helm/shardferry`.

## Quick start

Local, no external checkpoint required:

```bash
shardferry demo
shardferry doctor --checkpoint /data/job/source.dcp
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

# Redacted operator diagnostics for an issue report.
shardferry-support-bundle --namespace shardferry-control
```

`migrate` prints a real node/bytes transfer bar on stderr; use `--json` (or
`--no-progress`) for machine-driven runs.

`readiness` is read-only: it reports `complete`, `fresh`, `loadable` and
`movable` without modifying the checkpoint. A complete/fresh checkpoint is
still `UNVERIFIED` until a real restore drill or target load has run.

`restore-drill` requires a real target training command. The command must load
the checkpoint, run `--steps`, and write the target-ready report; the CLI does
not fake model steps.

`demo` exercises plan, transfer, bitwise verification and commit. `doctor` is
read-only. Migration never modifies the source checkpoint.

Kubernetes local-disk flow:

```bash
shardferryctl cluster add source \
  --endpoint https://source-gateway.example:9445 \
  --role source \
  --secret source-auth \
  --file clusters-source.yaml

shardferryctl preflight -f examples/k8s/migration.yaml --json
shardferryctl plan -f examples/k8s/migration.yaml --dry-run
shardferryctl migrate -f examples/k8s/migration.yaml --wait
shardferryctl describe migration job42-128-to-64 --ranks
shardferryctl readiness job42
shardferryctl rescue --name training-preempted --event Preempted --workload training \
  --checkpoint training-step-100 --target cluster-b
shardferryctl get rescuedecision training-preempted
shardferryctl kueue status
```

Cross-cluster installation, gateway discovery and per-cluster credentials are
documented in
[`examples/cross-cluster/README.zh-CN.md`](examples/cross-cluster/README.zh-CN.md).

## What 0.1.1 has actually been tested for

| Area | Evidence |
|---|---|
| Local DCP | demo, doctor, migrate, verify, target load |
| GPU FSDP/DCP | 4x NVIDIA L20; `4->2`, `4->3`, `4->1`, `1->4` fp32 and `4->2` bf16 |
| Larger GPU case | 769 MiB `4->3` checkpoint; bitwise target and source checksum stable |
| Kubernetes control plane | two kind clusters; 16->8 and synthetic 128->64 |
| Data path | 1 GiB synthetic local-disk flow; no full global gather |
| Recovery | target failure, controller restart, commit repair, resume journal |
| Security | per-cluster gateway tokens and end-to-end mTLS |
| Packaging | Python wheel, controller/agent/gateway images, Helm chart |

The 128->64 result is a control-plane and data-plane simulation, not a physical
128-GPU benchmark. GPU validation covers one 4-GPU node; cross-host GPU transfer
is still pending.

## What the 0.2.0-rc.1 candidate adds

| Area | Status |
|---|---|
| Kubernetes control plane | candidate; static and cross-mesh paths covered by CPU/kind tests |
| Checkpoint readiness | local readiness; K8s CheckpointRecord with multi-agent selector and 128-rank merge tests |
| Restore drill | real target-command CLI plus template-created K8s Indexed Job |
| Rescue decision | `restore/restart/migrate/hold` from readiness; generic Warning event watcher, no Kueue dependency |
| Mobility transaction | projects existing CheckpointMigration; `migrationSpec` creates an owned migration through the existing executor |
| Target-ready | candidate; Indexed Job, per-rank reports and fail-closed L4 state checks |
| Resume state | advanced; `state capture/verify/restore` backed by state bundles |
| LRS | experimental; Go live path + CPU contract worker; CPU 4-rank staged proof and GPU reconfigure pending |
| Rescue demo | local interrupt → journal resume → target load; kind fault matrix still pending |
| GPU live cutover | pending; 4-GPU NCCL window is required before any claim |

Detailed evidence:

- [`docs/GPU-VALIDATION-2026-10-22.md`](docs/GPU-VALIDATION-2026-10-22.md)
- [`docs/GPU-RUNBOOK.zh-CN.md`](docs/GPU-RUNBOOK.zh-CN.md)
- [`docs/BENCHMARK.zh-CN.md`](docs/BENCHMARK.zh-CN.md)
- [`docs/LIVE-ACCEPTANCE.zh-CN.md`](docs/LIVE-ACCEPTANCE.zh-CN.md)

## Safety contract

1. Source checkpoint trees are read-only.
2. No target rank is committed before its target bytes pass verification.
3. A failed rank cannot produce a successful overall status.
4. Target commits happen behind a rank-wide barrier.
5. Retry and resume never re-transfer already committed ranks.
6. Target cleanup is explicit; source deletion is out of scope.

## Compatibility and limits

- Supported checkpoint format: PyTorch DCP directory.
- Supported state: tensor shards plus DCP scalar/bytes metadata.
- Tested topology changes: 1D world-size resharding and the documented
  DeviceMesh placements.
- GPU test scope: model + AdamW state + forward smoke test, not a full training
  loop with dataloader/RNG/scheduler continuation.
- No production promise for online cutover, arbitrary tensor/pipeline
  parallelism, non-DCP formats, or every storage backend.
- Same-host performance is measured honestly; see
  [`docs/BENCHMARK.zh-CN.md`](docs/BENCHMARK.zh-CN.md).

## Repository layout

- `src/shardferry/`: Python CLI, planner, DCP adapter, executor, verifier,
  agents and proof.
- `cmd/` and `internal/`: Go controller, CLI and cross-cluster gateway.
- `api/` and `config/crd/`: `CheckpointMigration` and `ShardferryCluster` APIs.
- `deploy/`: Dockerfiles and Helm chart.
- `spec/` and `spec/v1/`: migration, plan, report, proof and V1 contracts.
- `examples/`: local and cross-cluster migration examples.
- `docs/`: operator, GPU, release, upgrade and troubleshooting guides.

## Development

```bash
python -m pip install -e ".[dev]"
pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/shardferry
python scripts/check_release.py --json
```

The checkpoint-aware preemption guard is an independently deployable adapter,
with a Helm component, CheckpointRecord-first observation, strict profile
validation and `shardferryctl kueue` status. See
[`docs/KUEUE-PREEMPTION-GUARD.zh-CN.md`](docs/KUEUE-PREEMPTION-GUARD.zh-CN.md)
and [`examples/kueue-adapter`](examples/kueue-adapter) for its exact,
deliberately narrow boundary.

Release instructions are in [`docs/RELEASE.zh-CN.md`](docs/RELEASE.zh-CN.md).
Contributions follow [`CONTRIBUTING.md`](CONTRIBUTING.md). Security reports
should use GitHub private vulnerability reporting; see [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
