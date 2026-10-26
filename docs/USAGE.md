# Usage

This page covers the main Aevrel workflows. Start with
[Quick start](QUICKSTART.md) for installation and the first DCP demo.

## Which surface am I using?

Two command surfaces share the same vocabulary but not the same scope:

| Surface | Runs against | Use it for |
|---|---|---|
| `aevrel` (Python, local) | a checkpoint path on the machine you are on | read-only inspection, readiness, restore drills, local migration, resume state, the TUI |
| `aevrelctl` (Go, cluster) | a Kubernetes cluster and the agents registered in it | migration resources, plan and preflight, rescue decisions, Kueue facts, cluster inventory |

Use `aevrel` when the checkpoint is a directory you can read. Use `aevrelctl`
when the checkpoint sits behind a cluster and the decision has to be recorded
as a resource that other people and the scheduler can see.

`readiness` and `migrate` exist on both surfaces and answer different
questions:

- `aevrel readiness --checkpoint /path/to/checkpoint --target-world 3` inspects
  that path, here and now, and prints the verdict.
- `aevrelctl readiness NAME` asks the cluster what it recorded for a workload.
- `aevrel migrate --checkpoint ... --output ...` writes a new checkpoint tree
  locally. `aevrelctl migrate -f migration.yaml` submits a migration resource
  and can `--dry-run` or `--wait` on it.

## Local checkpoint inspection

```bash
python -m aevrel readiness \
  --checkpoint /data/job/checkpoint \
  --target-world 3 \
  --json
```

The command is read-only. Without a restore drill or target-load proof,
`loadable` is unknown and the status is `UNVERIFIED`.

## Restore drill

The target command must load model and optimizer state and run the requested
steps:

```bash
python -m aevrel restore-drill \
  --checkpoint /data/job/checkpoint \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --steps 10 \
  --report ./reports/rank0.json
```

For multiple ranks, create one report per rank and attach the directory:

```bash
python -m aevrel readiness \
  --checkpoint /data/job/checkpoint \
  --target-world 3 \
  --drill-report ./reports
```

Every expected rank must be represented. A partial report set remains
unverified.

### Reason codes, rank coverage, and movable

A verdict never rounds up. Every claim it withholds is named by a reason
code (`DRILL_MISSING`, `DRILL_STEPS_MISSING`,
`DRILL_RANK_COVERAGE_INCOMPLETE`, `DRILL_RANK_COVERAGE_UNREPORTED`, ...):
`null` means no evidence, `false` means the evidence says no, and the two
are never interchangeable.

Rank coverage is measured against the checkpoint's own inventory, so a
drill must report every expected rank. A drill that names no ranks is
*unreported*, not zero, and stays non-`READY`; a partial one names the
missing ranks so an operator can act on them.

`movable` is a coarse filter — completeness and target world size only —
not a topology verdict. When the target world size differs from the
source, the verdict attaches `TARGET_TOPOLOGY_UNVERIFIED`; mesh, placement
and format compatibility need a real target load. The full catalog:
[Failure codes](FAILURE-CODES.md). The normative field semantics:
[Readiness semantics](READINESS-SEMANTICS.md).

## Resume-state closure (L3 vs L4)

Pass the manifest the checkpoint actually stores to bind the verdict to one
manifest, and add `--require-state-complete` to claim L4:

```bash
python -m aevrel readiness \
  --checkpoint /data/job/checkpoint \
  --drill-report ./reports \
  --resume-state ./resume-state.json \
  --require-state-complete \
  --strict
```

Without the flag the assessment stays an L3 claim and reports
`state_complete` and `readiness_level` for consumers to act on. With the flag:

- an incomplete closure blocks the result (`TARGET_BLOCKED`,
  `STATE_CLOSURE_INCOMPLETE`) and retracts `loadable`;
- the drill reports must carry the same manifest digest as the assessed
  manifest, otherwise `RESUME_COMPONENT_DIGEST_MISMATCH` blocks the result;
- a manifest that describes another checkpoint, version or metadata digest is
  rejected as `RESUME_MANIFEST_INVALID`;
- `--require-state-complete` without `--resume-state` exits 2: an L4 claim must
  be anchored to one manifest.

Only an anchored, complete closure on a `READY` checkpoint reports
`readiness_level: L4`.

## Rank-local resume state (`aevrel state`)

The `aevrel state` command captures, verifies, and restores the rank-local
resume state (dataloader position, RNG and sampler state) that the L4
claim is built on:

```bash
python -m aevrel state capture \
  --root /data/job/resume-state \
  --checkpoint-id ckpt-7 \
  --checkpoint-version v7 \
  --rank 0 \
  --source-world 4

python -m aevrel state verify --root /data/job/resume-state --require-complete

python -m aevrel state restore --root /data/job/resume-state --target-rank 0
```

`capture` writes the manifest under `--root`; `verify` checks it (with
`--require-complete` it fails while any required component is missing);
`restore` reinstates one rank's state on the target. The manifest format
and proof binding: [Resume state](RESUME-STATE.md).

## DCP migration

```bash
python -m aevrel migrate \
  --checkpoint /data/job/source.dcp \
  --target-world 3 \
  --output /data/job/target-3rank.dcp
```

The source is read-only. The target is written to a staging area and committed
only after verification. Interrupted work can resume from the journal.

Mesh-aware migration:

```bash
python -m aevrel migrate \
  --checkpoint /data/job/source.dcp \
  --source-mesh-shape 2,2 \
  --source-placements "Shard(0),Shard(1)" \
  --target-mesh-shape 3,1 \
  --target-placements "Shard(0),Replicate()" \
  --output /data/job/target-3rank.dcp
```

## Kubernetes control plane

```bash
go build -o bin/aevrelctl ./cmd/ctl
export PATH="$PWD/bin:$PATH"

kubectl apply -f examples/checkpoint-control/checkpoint-record-selector.yaml
aevrelctl readiness training-step-100
```

Offline manifest validation:

```bash
aevrelctl migrate -f examples/k8s/migration.yaml --dry-run
```

Inspect and control migrations:

```bash
aevrelctl describe migration <name> -o json
aevrelctl migrate -f migration.yaml --wait
aevrelctl retry migration <name>
aevrelctl cancel migration <name>
```

## Kueue

The optional adapter publishes checkpoint protection facts to Kueue. See
[Kueue preemption guard](KUEUE-PREEMPTION-GUARD.md).

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true

aevrelctl kueue status
aevrelctl kueue explain <workload>
```

## Target validation

The target-ready Job contract checks per-rank model/optimizer load and training
steps. See [Target-ready](TARGET-READY.md).

## Live Rank Split

LRS is experimental. See [Live Rank Split workflow](LIVE-RANK-SPLIT-USAGE.md)
and [LRS worker](LRS-WORKER.md) for the supported CPU-contract path.

## Automation and CI

The TUI is interactive and needs a terminal. In CI, scripts, and cron, use
the command surface with `--json` and treat exit code 2 as "not ready or
failed":

```bash
python -m aevrel readiness --checkpoint /data/job/checkpoint --json
```

`NO_COLOR` (or `--no-color`) keeps every text surface plain, uncolored, and
diffable.

## Troubleshooting

- Missing DCP metadata: verify the checkpoint path and migration status.
- Missing rank shard: inspect the target rank mapping; do not copy another
  rank's files by hand.
- Shape or dtype mismatch: verify the target model structure, world size, and
  DeviceMesh.
- Full-load OOM: use a rank-local smoke first.
- Unexpected source changes: preserve the source and collect a support bundle.
