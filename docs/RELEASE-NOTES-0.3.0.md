# Aevrel 0.3.0

Release date: 2026-10-23

This is the Aevrel identity release. It introduces the training-state control
plane and the practical local/Kubernetes workflows built around it under the
new project name.

## Highlights

- **Training-state control plane:** `CheckpointRecord`, readiness,
  `RestoreDrill`, `RescueDecision`, `CheckpointMigration`, conditions, and
  proof-oriented status.
- **Aevrel identity:** the Go module, Python package and CLI, Kubernetes API
  group, Helm chart, container images, and environment variables now use
  Aevrel.
- **Local CLI and terminal control plane:** `aevrel demo`,
  `demo --dcp`, `doctor`, `capabilities`, `readiness`, `restore-drill`,
  `migrate`, `state`, `target-load`, `target-report`, `support-bundle`, and
  the local TUI.
- **Safe DCP migration:** source read-only, range reads, bitwise verification,
  resumable journals, and all-or-nothing target commit.
- **Kubernetes execution:** `CheckpointMigration` controller, source/target
  agents, selector discovery, preflight, plan preview, rank-level describe,
  gateway, per-cluster tokens, and mTLS.
- **Target validation:** target-ready Indexed Jobs, per-rank reports,
  model/optimizer load, N training steps, and fail-closed status.
- **Rescue flow:** generic Kubernetes Warning events can produce
  restore/restart/migrate/hold decisions without requiring Kueue.
- **Rank-local state:** capture, verify, restore, RNG/sampler continuity, and
  state-closure validation.
- **Kueue adapter:** optional Helm deployment, CheckpointRecord-first
  observation, strict profile validation, and
  `aevrelctl kueue status|explain`.
- **Live Rank Split contract:** on-demand `aevrel-live-worker` with
  durable tick state, coverage/quorum checks, and CPU-contract proof.
- **Packaging and operations:** Helm chart, controller/gateway/agent/
  live-worker/kueue-adapter images, wheel, release checks, support bundle, and
  public architecture/usage documentation.

## Current boundaries

- DCP is the primary supported checkpoint format.
- No production SLA is implied by this release.
- Real multi-node Kubernetes, large-scale GPU, and cross-machine GPU transfer
  still require environment-specific validation.
- LRS is experimental; the current worker is a CPU-contract backend and does
  not prove GPU collective continuity.
- `Kueue` is an adapter. aevrel does not replace Kueue scheduling.
- Arbitrary CUDA context, Python heap, or GPU process hot migration is not
  claimed.

## Install

Install the wheel from the release assets:

```bash
python -m pip install aevrel-0.3.0-py3-none-any.whl
```

For DCP commands, install PyTorch in the same environment:

```bash
python -m pip install torch
```

The Helm chart and container images are published by the tag-triggered release
workflow.

## Start here

```bash
aevrel demo
aevrel demo --dcp
aevrel tui
```

Then read [Quick start](QUICKSTART.zh-CN.md) and
[Usage](USAGE.zh-CN.md).

## Notes

The old `0.1.x` release history and tags were removed during the clean-room
repository rebuild. This note describes the capabilities present in the
current source tree, not a continuation of the old release line.
