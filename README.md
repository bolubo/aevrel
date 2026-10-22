# shardferry

[![CI](https://github.com/bolubo/shardferry/actions/workflows/ci.yml/badge.svg)](https://github.com/bolubo/shardferry/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bolubo/shardferry?include_prereleases)](https://github.com/bolubo/shardferry/releases)

**Move and reshard local checkpoint shards across clusters and DeviceMeshes.**

shardferry is a DCP-native checkpoint mobility layer for training clusters. It
moves checkpoint bytes from per-rank local disks to a target topology without
requiring a global checkpoint directory, an S3 copy, or a source restart.

```text
source shards stay read-only
        -> topology-aware plan
        -> direct range transfer
        -> staging + bitwise verification
        -> all-or-nothing commit
        -> loadable target DCP
```

Status: **0.1.1 Alpha**. The local DCP path and the Kubernetes control-plane path
are usable within the boundaries below. This is not a production SLA and does
not promise online/live training cutover.

Roadmap: [`0.1.x` static checkpoint mobility](docs/0.1-ACCEPTANCE.zh-CN.md) is
released; [`0.2.0` Live Handoff](docs/0.2-ACCEPTANCE.zh-CN.md) is the next major
capability. See the full [`roadmap`](docs/ROADMAP.zh-CN.md).

## Why

A 128-rank training job can leave DCP shards on local NVMe, with no shared
checkpoint filesystem. A recovery or cluster-replacement job may need to resume
on 64 ranks. shardferry:

- discovers source and target ranks instead of requiring 128 hand-written
  mappings;
- reads tensor ranges directly from `.distcp` files;
- reshards across target world sizes and DeviceMeshes;
- resumes after interrupted transfers;
- verifies target bytes and requires all ranks to commit together;
- returns a target DCP that the target training job can load.

The project does **not** try to win same-host raw-copy benchmarks. Native DCP
load-time resharding is faster when source and target already share a
filesystem. shardferry's value is cross-domain movement, resumability, proof,
and failure-safe commit.

## Install

### Python wheel

Download the `shardferry-0.1.1-py3-none-any.whl` asset from the GitHub release:

```bash
python -m pip install shardferry-0.1.1-py3-none-any.whl
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
go build ./cmd/controller ./cmd/ctl ./cmd/gateway
helm lint deploy/helm/shardferry
python scripts/check_release.py --json
```

Release instructions are in [`docs/RELEASE.zh-CN.md`](docs/RELEASE.zh-CN.md).
Contributions follow [`CONTRIBUTING.md`](CONTRIBUTING.md). Security reports
should use GitHub private vulnerability reporting; see [`SECURITY.md`](SECURITY.md).

## License

Apache-2.0. See [`LICENSE`](LICENSE).
