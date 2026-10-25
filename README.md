# Aevrel

[![CI](https://github.com/bolubo/aevrel/actions/workflows/ci.yml/badge.svg)](https://github.com/bolubo/aevrel/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bolubo/aevrel?include_prereleases)](https://github.com/bolubo/aevrel/releases)
[![License](https://img.shields.io/github/license/bolubo/aevrel)](LICENSE)

**Scheduler-neutral training-state control plane for AI infrastructure.**

Kubernetes and AI schedulers see pods, GPUs, quotas, and priorities. Training
runtimes see ranks, shards, DeviceMeshes, checkpoint versions, recovery cost,
and whether a target can actually load the state. Aevrel provides the
portable state contract between those two worlds.

```text
training runtime + storage
        -> checkpoint / runtime facts
        -> completeness, freshness, loadability, movability
        -> rescue decision or mobility action
        -> proof
        -> scheduler / operator / UI
```

Checkpoint readiness is the first complete vertical. Transfer is one action;
Kueue and Live Rank Split (LRS) are adapters, not alternate sources of truth.

## What it does

| Capability | Question answered | Main surface |
|---|---|---|
| Inventory | Which checkpoint/version exists, and are all ranks/shards present? | `CheckpointRecord`, `aevrel readiness` |
| Readiness | Is it complete, fresh, loadable, and movable? | `CheckpointReadiness` |
| Verification | Did a real target load and N training steps succeed? | `RestoreDrill`, `restore-drill` |
| Rescue decision | After preemption/failure, should we restore, restart, migrate, or hold? | `RescueDecision` |
| Mobility | Can A move to B, and can a different world size/mesh continue training? | `CheckpointMigration` |
| Proof | Why should an operator or scheduler trust the result? | digest, coverage, load proof, timeline |
| Integration | How do schedulers and platforms consume the facts? | Kueue adapter, CRDs, `aevrelctl` |

The project owns the state semantics and safety boundaries. It reuses DCP,
Kubernetes, storage systems, schedulers, and training frameworks instead of
reimplementing them. It is not a replacement for Kueue, Volcano, Kubeflow, or
Kubernetes scheduling; not a checkpoint format or a model-training framework;
and not a file-copy utility without a readiness and proof model.

## Five-minute path (local, CPU, no cluster)

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev,dcp]"

# Toy migration, dependency-free
aevrel demo

# Real CPU DCP pair plus target-load proof
aevrel demo --dcp --work-dir /tmp/aevrel-first-run --json
```

Seeded expectations for `aevrel demo --dcp`: `verification.passed=true`,
source and target readiness `READY`, and `dcp.target_load=ok`. Then point the
same tools at a real checkpoint tree:

```bash
aevrel readiness --checkpoint /data/job/source.dcp --target-world 3
aevrel tui                                   # terminal control plane
```

`restore-drill` proves a real target load plus N training steps; readiness
only becomes `READY` with a complete set of rank drill reports, and
`aevrel-target-load` gives the rank-local load proof. Full walkthrough:
[Quick start](docs/QUICKSTART.md) and
[Usage and workflows](docs/USAGE.md).

## Status

[0.3.0](https://github.com/bolubo/aevrel/releases/tag/v0.3.0) is the current
release: the local CPU path and control-plane contracts are usable, while real
Kubernetes, multi-node, and GPU validation remain environment-specific. There
is no production SLA, source checkpoint trees are read-only, and LRS is
experimental. The source of truth for current claims is the docs and release
notes, not issue comments or old design notes.

## Where things live

```text
src/aevrel/       Python CLI, DCP adapter, agents, readiness, state, LRS
cmd/ internal/    Go controller, ctl, gateway, kueue adapter
api/ + config/    CRD types and generated definitions
spec/             cross-language contract schemas
deploy/ examples/ Dockerfiles, Helm chart, user-facing manifests
docs/             product, usage, operator and integration docs
```

## Go further

- **Users:** [Quick start](docs/QUICKSTART.md) ·
  [Usage and workflows](docs/USAGE.md) ·
  [Target-ready validation](docs/TARGET-READY.md) ·
  [Target DCP load](docs/TARGET-LOAD.md)
- **Cluster operators:** [Helm installation](deploy/helm/aevrel/README.md) ·
  [Kueue preemption guard](docs/KUEUE-PREEMPTION-GUARD.md) ·
  [Support bundle](docs/SUPPORT.md) (`aevrel-support-bundle`)
- **Contributors:** [First contribution](docs/FIRST-CONTRIBUTION.md) ·
  [Development map](docs/DEVELOPMENT-MAP.md) ·
  [Architecture](docs/ARCHITECTURE.md) ·
  [Roadmap](docs/ROADMAP.md) ·
  [RFC guide](docs/RFC-GUIDE.md)
- **Everything:** [Documentation index](docs/INDEX.md)

## Community and governance

[Contributing](CONTRIBUTING.md) · [Governance](GOVERNANCE.md) ·
[Maintainers](MAINTAINERS.md) ·
[Contributor ladder](CONTRIBUTOR_LADDER.md) ·
[Triage policy](TRIAGE.md) · [Support](SUPPORT.md) ·
[Security policy](SECURITY.md) · [Release process](RELEASING.md) ·
[Code of Conduct](CODE_OF_CONDUCT.md) ·
[Good first issues](https://github.com/bolubo/aevrel/labels/good%20first%20issue)

Use GitHub issues for bugs, usage questions, feature requests, and integration
requests. Do not post secrets or checkpoint contents.

## Validate a change

```bash
python scripts/check_repo.py     # repository and documentation hygiene
python -m pytest -q              # Python suite
go test ./...                    # Go suite
```

The full contributor workflow lives in
[Development map](docs/DEVELOPMENT-MAP.md). History:
[Changelog](CHANGELOG.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
