# shardferry 0.2.0-rc.1 — Mobility Control Plane Candidate

This is a release candidate, not a published production release. The last
published release remains `0.1.1` until the GPU and cross-machine gates pass.

## Highlights

- Static local-disk DCP migration with bitwise verification and atomic commit.
- Kubernetes `CheckpointMigration` control plane for local-disk, cross-mesh
  migration and target-ready validation.
- Target-ready Indexed Jobs with per-rank reports and fail-closed status.
- Rank-local state bundle capture, verification and restore.
- CPU 4-rank LRS proof and a torchcomms reconfigure preflight.
- Clear experimental boundary for live rank split.
- Real transfer progress for local migrations and rank/ownership progress for
  control-plane migrations; LRS concurrency is user-configurable with
  `maxInFlight` / `--max-in-flight`.
- A read-only `shardferry readiness` command and Go control-plane models for
  CheckpointRecord/Readiness/RestoreDrill/RescueDecision.
- A target-command `restore-drill` path that requires real model/optimizer
  load and N training steps, not a metadata-only check.
- RestoreDrill control-plane Jobs, multi-agent `CheckpointRecord` scans up to
  128 ranks, and rescue decisions generated from generic Kubernetes Warning
  events without requiring Kueue.
- Drill proof is bound to checkpoint version + metadata digest; a changed
  checkpoint invalidates the old `loadable=true`.
- An on-demand `shardferry-live-worker` implements
  `POST /v1/live/reconcile` with durable tick state and CPU-contract proof.
  It is not a GPU collective proof; the target-ready gate remains independent.
- The Kueue preemption guard is installable as an optional Helm component,
  consumes `CheckpointRecord` readiness by default, validates the strict
  profile, and exposes `shardferryctl kueue status|explain`.
- A local rescue demo that interrupts a real migration, preserves the journal,
  resumes the same command and verifies the target.

## Not yet proven

- 4-rank NCCL reconfigure and a real 4-GPU live rank split.
- Cross-machine/跨集群 live rank split.
- Full training-loop continuity across arbitrary frameworks and schedulers.
- Multi-rank local restore-drill execution outside Kubernetes.
- Direct LRS worker integration beyond the CPU contract backend.
- Kueue integration; that workstream is owned separately.

## Upgrade

Use the `0.1.1` release for the currently published static path. Use this
candidate from source only after the internal acceptance gate passes.
