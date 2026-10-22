# Changelog

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
