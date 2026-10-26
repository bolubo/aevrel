# Aevrel 0.4.0

Release date: 2026-10-26

This is the evidence release. Every readiness claim is now bound to the
evidence behind it, invalidated when that evidence stops holding, and rendered
the same way on every surface. The three roadmap focus areas — resume-state
closure, the scheduler/runtime contract, and readiness verification — are
implemented in this release.

## Highlights

- **Readiness evidence semantics:** a drill that ran no training steps
  (`DRILL_STEPS_MISSING`), covered only part of the world
  (`DRILL_RANK_COVERAGE_INCOMPLETE`), or reported no ranks at all
  (`DRILL_RANK_COVERAGE_UNREPORTED`) no longer counts as `loadable`. A proof
  produced for another version or metadata digest is `DRILL_PROOF_STALE`.
  See [Readiness semantics](READINESS-SEMANTICS.md).
- **L4 resume-state closure:** `readiness --require-state-complete` gates the
  L4 claim on a complete, anchored resume-state closure; a drill that
  restored a different manifest is rejected
  (`RESUME_COMPONENT_DIGEST_MISMATCH`, `RESUME_MANIFEST_INVALID`).
- **Rank coverage as identities:** drill reports carry `ranks_reported`
  against `ranks_expected`, so a verdict names the ranks that did not come
  back instead of only counting them.
- **Scheduler/runtime contract v1:** a versioned, orthogonal fact envelope
  (`schemaVersion: aevrel.dev/v1`) with a fail-closed conformance gate;
  `aevrelctl kueue explain` reports the facts in text and `--json`, and
  `kueue-adapter --envelope-labels` publishes them as advisory labels.
- **`movable` is labelled a coarse filter** wherever it is printed, including
  `TARGET_TOPOLOGY_UNVERIFIED` when the target world size differs from the
  source's rank count.
- **Discoverability:** `aevrel --help` lists every subcommand with help text;
  the TUI detail views render the closure verdict, the L3/L4 level, rank
  coverage, and the full reason list; README, [Quick start](QUICKSTART.md)
  and [Usage](USAGE.md) explain how to read a verdict and link
  [Readiness semantics](READINESS-SEMANTICS.md) and
  [Failure codes](FAILURE-CODES.md).
- **Fixed:** the native CAS ingest no longer fails when two worker threads
  ingest files with identical content concurrently.

## Upgrading from 0.3.0

- `aevrel demo` without `--work-dir` now writes to a unique temp directory
  instead of `./aevrel-demo` in the current directory.
- Verdicts can move from `READY` to a not-ready status where evidence is
  missing — that is the fail-closed semantics doing their job, not a
  regression. Read the reason codes; every withheld claim names itself.
- `movable=true` text output carries a `(coarse: ...)` label; consumers that
  parse fixed-width CLI output should match on the field, not the line.
- `aevrel --help` now lists all subcommands, including the
  `inspect`/`plan`/`apply`/`verify` workflow.

## Current boundaries

- DCP is the primary supported checkpoint format.
- No production SLA is implied by this release.
- Real multi-node Kubernetes, large-scale GPU, and cross-machine GPU transfer
  still require environment-specific validation.
- LRS is experimental; the current worker is a CPU-contract backend and does
  not prove GPU collective continuity.
- `Kueue` is an adapter. Aevrel does not replace Kueue scheduling.
- The Volcano and Kubeflow translators are reserved interfaces and report
  themselves as unsupported.

## Install

Install the wheel from the release assets:

```bash
python -m pip install aevrel-0.4.0-py3-none-any.whl
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
aevrel readiness --checkpoint /path/to/checkpoint --target-world 3
aevrel tui
```

Then read [Quick start](QUICKSTART.md) and [Usage](USAGE.md). Every failing
command ends with an explicit result line and reason codes; the catalog for
those codes is [Failure codes](FAILURE-CODES.md).
