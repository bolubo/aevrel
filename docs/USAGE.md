# Usage

This page covers the main Aevrel workflows. Start with
[Quick start](QUICKSTART.md) for installation and the first DCP demo.

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

## Troubleshooting

- Missing DCP metadata: verify the checkpoint path and migration status.
- Missing rank shard: inspect the target rank mapping; do not copy another
  rank's files by hand.
- Shape or dtype mismatch: verify the target model structure, world size, and
  DeviceMesh.
- Full-load OOM: use a rank-local smoke first.
- Unexpected source changes: preserve the source and collect a support bundle.
