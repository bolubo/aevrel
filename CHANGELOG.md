# Changelog

## Unreleased

### Added

- Repository governance and community entry points: Code of Conduct,
  governance model, maintainer roster, contributor ladder, triage policy,
  support, release process, CODEOWNERS, and expanded issue templates.
- `shardferry demo --dcp` creates a real source/target DCP pair and verifies
  target load.
- `shardferry readiness --drill-report` accepts target-ready reports and
  requires all checkpoint ranks before reporting `READY`.

### Changed

- Public positioning now describes a scheduler-neutral training-state control
  plane; checkpoint readiness is the first complete vertical.
- `shardferryctl plan --dry-run` is no longer advertised as an offline mode;
  use `shardferryctl migrate -f FILE --dry-run` without a cluster.

### Removed

- Internal worklog, obsolete charter/verification notes, and legacy
  compatibility paths from the public repository.

## 0.2.0-rc.1 - 2026-10-22

Checkpoint mobility control-plane release candidate.

- Added the `CheckpointMigration` control-plane path for local-disk,
  cross-mesh migration with target-ready validation.
- Added target-ready Indexed Job handling, per-rank reports, state closure
  checks and fail-closed target status.
- Added rank-local state bundle capture/verify/restore and `RankSampler`
  continuity for resume validation.
- Added CPU 4-rank LRS proof and a torchcomms reconfigure preflight; live
  rank split remains experimental until real 4-GPU validation.
- Added the first checkpoint-control-plane slice: local `shardferry readiness`,
  Go readiness models/CRDs for CheckpointRecord/RestoreDrill/RescueDecision,
  and cross-language schemas.
- Added a real target-command `shardferry restore-drill` path that requires
  model/optimizer load and N training steps before accepting the drill proof.
- Added template-created Indexed restore-drill Jobs, multi-agent
  `CheckpointRecord` scanning with 128-rank merge tests, and
  version/digest-bound drill evidence.
- Added fail-closed `RescueDecision` policy plus a generic Kubernetes Warning
  event watcher; rescue works without Kueue.
- Added `shardferry-live-worker`, a durable `/v1/live/reconcile` CPU contract
  worker with phase/coverage/quorum/proof output; the Go live path now has a
  concrete on-demand worker instead of failing on a missing service.
- Productized the Kueue adapter: Helm deployment/RBAC/profile, optional
  CheckpointRecord-first observation, strict profile validation, and
  `shardferryctl kueue status|explain`.
- Added release-candidate version, capability and experimental-surface
  documentation.

Known limitations:

- The 4-GPU NCCL reconfigure and cross-machine live path are not yet proven.
- The CPU4 proof uses staged `destroy/reinit`, not live collective continuity.
- Kueue integration is a separate workstream and is not part of this candidate.

## 0.1.1 - 2026-10-22

GPU-validated alpha release.

- Validated real FSDP/DCP target loads on 4x NVIDIA L20:
  `4->2`, `4->3`, `4->1`, `1->4` fp32 and `4->2` bf16.
- Added a 769 MiB `4->3` GPU checkpoint case with source checksum preservation,
  bitwise migration verification and target load.
- Fixed PyTorch 2.11 DCP compatibility for concatenated `.distcp` storage
  segments, padded tensor payloads, `BytesStorageMetadata` optimizer fields and
  zero-dimensional optimizer step tensors.
- Added GPU preflight, short step-by-step runbook and reproducible harnesses.
- Added cross-cluster gateway, per-cluster gateway tokens and end-to-end mTLS.
- Added cluster health, rank-level migration status, migration plan preview and
  safe cancel/retry/delete semantics.
- Hardened controller restart recovery, report evidence and target cleanup.
- Added per-rank load readiness, migration proof and support-bundle tooling.
- Published release gates for Python, Go, Helm, wheel and chart installation.

Known limitations:

- GPU validation covers one 4-GPU node; physical 128-rank GPU clusters and
  cross-host GPU transfer are not yet validated.
- The 128-rank/1 GiB Kubernetes result is a control-plane/data-plane simulation,
  not a 128-GPU performance claim.
- On one host with a shared filesystem, native DCP load-time resharding is
  faster; shardferry's differentiator is cross-domain movement, resume and
  proof, not same-host raw speed.
- Target readiness is a model/optimizer load and forward smoke test. Full
  training-loop continuity (dataloader, RNG, scheduler) is not part of 0.1.1.
- Alpha software: no production SLA and no online live-cutover promise.

## 0.1.0 - 2026-10-22

Preview release.

- Local `shardferry demo`, `doctor`, and DCP `migrate` path.
- DCP range reads without a global gather checkpoint.
- Kubernetes `CheckpointMigration` controller and source/target agents.
- `agentSelector` discovery with local rank reporting.
- `shardferryctl migrate --dry-run/--wait` and `preflight`.
- Per-rank target DCP finalization with rank-local load smoke.
- Helm chart, CPU-DCP agent image, and controller image build definitions.
- Bitwise verification, resumable journals, and all-or-nothing commit barrier.

Known limitations:

- Physical multi-node and GPU FSDP acceptance is not yet recorded.
- Kubernetes local-disk selector flow needs a real multi-node kind/cluster run.
- The public preview is not a production SLA or a complete scheduler.
