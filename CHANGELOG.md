# Changelog

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
