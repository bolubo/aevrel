# Changelog

## Unreleased

### Added

- Readiness evidence semantics: a fact is now bound to the evidence behind it
  and invalidated when that evidence stops holding. A restore drill that ran no
  training steps (`DRILL_STEPS_MISSING`) or that covered only part of the world
  (`DRILL_RANK_COVERAGE_INCOMPLETE`) no longer counts as `loadable`, and a proof
  produced for another version or metadata digest is reported as
  `DRILL_PROOF_STALE` instead of being conflated with a failed drill. See
  [docs/READINESS-SEMANTICS.md](docs/READINESS-SEMANTICS.md).
- Rank coverage is carried as identities, not a count: a drill reports
  `ranks_reported` against `ranks_expected`, so a verdict can name the ranks
  that did not come back instead of only counting them. A drill that reports
  **no** rank at all is `DRILL_RANK_COVERAGE_UNREPORTED`: silence is not
  coverage, and it is no longer read as "0 of 0, therefore fine".
- The manifest identity anchor (`RESUME_MANIFEST_INVALID`) is enforced on the Go
  side as well: agents report the `checkpoint` their manifest describes, the
  record persists it, and a complete closure for another checkpoint can no
  longer vouch for this one.
- `TARGET_TOPOLOGY_UNVERIFIED` marks `movable=true` as a coarse check when the
  target world size differs from the source's rank count.
- Scheduler/runtime contract v1: a versioned, orthogonal fact envelope for
  `complete`, `fresh`, `loadable`, `movable`, `restoreMode`,
  `estimatedRTOSeconds`, and the evidence behind them. See
  [docs/scheduler-runtime-contract-v1.md](docs/scheduler-runtime-contract-v1.md).
- `aevrelctl kueue explain` now reports the authoritative checkpoint facts, in
  text and as a stable `--json` contract (`schemaVersion: aevrel.dev/v1`).
- `kueue-adapter --envelope-labels` publishes the envelope as advisory
  `aevrel.dev/*` labels. It is off by default and the protection decision is
  identical either way.
- A conformance gate that withholds the envelope when its facts contradict each
  other, when a fact set cannot describe itself, or when a source claims values
  it does not have.

### Changed

- The failure-code catalog now documents the rescue advisory codes and the
  envelope violation codes, and `repo-check` fails when an envelope label is
  published without being documented.
- `RestoreWithoutDrillProof` now requires a *bound* proof: an envelope that
  recommends `restore` with a drill name but no `lastDrillDigest` is withheld,
  because a name cannot be invalidated when the checkpoint changes.
- `aevrel readiness` labels `movable=true` as a coarse check in its text output.

### Fixed

- The native CAS ingest no longer fails when two worker threads ingest files
  with identical content at the same time. Chunks are written to a private
  temp file and renamed into place, so a concurrent reader sees either no
  chunk or the whole chunk. A partial chunk left behind by an interrupted
  writer is repaired from the bytes being ingested instead of aborting the
  ingest with `existing CAS chunk has wrong size`.

## 0.3.0 - 2026-10-23

Aevrel identity release. The project, Go module, Python package and CLI,
Kubernetes API group, Helm chart, container images, and environment variables
now use the Aevrel name.

### Added

- Training-state control plane resources: `CheckpointRecord`, readiness,
  `RestoreDrill`, `RescueDecision`, and `CheckpointMigration`.
- Local CLI and terminal control plane: demo, real DCP demo, readiness,
  restore drill, migration, state bundles, target load/report, support bundle,
  and TUI.
- DCP range reads, bitwise verification, resumable journals, and atomic target
  commit while keeping source checkpoints read-only.
- Kubernetes controller/agent flow with selector discovery, preflight, plan
  preview, rank-level status, gateway, per-cluster tokens, and mTLS.
- Target-ready Indexed Jobs with per-rank reports, model/optimizer load,
  N training steps, and fail-closed validation.
- Rescue decisions from generic Kubernetes Warning events, without requiring
  Kueue.
- Optional Kueue adapter with CheckpointRecord-first readiness, strict profile
  validation, and `aevrelctl kueue status|explain`.
- CPU-contract LRS worker and live-rank-split control path; GPU collective
  continuity remains experimental.
- Rank-local state closure, RNG/sampler continuity, and restore verification.
- Helm chart, container image definitions, wheel packaging, support bundle,
  release checks, governance docs, and good-first-issue workflow.

### Changed

- Public positioning is now a scheduler-neutral training-state control plane.
- Replaced the previous project identity across code, package names, CLI,
  CRDs, API group, Helm chart, images, documentation, and tests.
- The roadmap focuses on resume-state closure, scheduler/runtime contracts,
  and checkpoint readiness/verification.
- Commit and PR guidance uses plain, descriptive subjects and standard
  Summary/Why/Validation sections.

### Removed

- Legacy warm-handoff/cutover paths and compatibility shims.
- Internal worklogs, obsolete charters, research archives, and non-product
  documentation from the public repository.

### Known limitations

- No production SLA.
- DCP is the primary supported checkpoint format.
- Real multi-node Kubernetes, large-scale GPU, and cross-machine GPU transfer
  still require environment-specific validation.
- LRS does not yet prove GPU collective continuity.
- Kueue is an adapter; Aevrel does not replace scheduler policy.
- Arbitrary CUDA context or Python heap hot migration is not claimed.
