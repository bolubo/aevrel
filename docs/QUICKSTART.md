# Quick Start

This guide runs the existing Aevrel DCP path. It does not require a GPU or a
Kubernetes cluster. Every command ends with an explicit result line, so a
terminal session is never ambiguous about what was proven.

## 1. Install

Python 3.11 or newer is required.

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
```

## 2. Run the DCP demo

```bash
aevrel demo --dcp --work-dir /tmp/aevrel-demo-dcp
```

Real output from that command:

```text
─  AEVREL · DEMO  ──────────────────────────────────────────────
Demo complete
  work dir       /tmp/aevrel-demo-dcp
  target         /tmp/aevrel-demo-dcp/target
  verification   bitwise passed
  dcp source     /tmp/aevrel-demo-dcp/source.dcp
  dcp target     /tmp/aevrel-demo-dcp/target-3.dcp
  dcp load       source READY, target READY
  target load    ok
  next           aevrel-target-load --checkpoint /tmp/aevrel-demo-dcp/target-3.dcp --rank 0 --json
RESULT: OK
```

The demo creates a source DCP checkpoint, migrates it to a different world
size, loads the target, and verifies that the source remains unchanged.
`aevrel demo` without `--dcp` runs the dependency-free toy path; add
`--json` on either form for machine-readable output.

## 3. Inspect a checkpoint

```bash
aevrel readiness \
  --checkpoint /tmp/aevrel-demo-dcp/source.dcp \
  --target-world 3
```

Real output, before any drill has verified this checkpoint:

```text
─  AEVREL · READINESS  ─────────────────────────────────────────
  checkpoint   /tmp/aevrel-demo-dcp/source.dcp
  provider     dcp
  version      source.dcp
  ranks        4
  complete     True
  fresh        True
  loadable     unknown
  movable      True
  status       ?? UNVERIFIED
  DRILL_MISSING: no restore drill has verified this checkpoint
RESULT: NOT READY
NEXT: run a restore drill / target load before treating this checkpoint as recoverable
```

`complete=true` and `fresh=true` only describe inventory. Without a real
validation report, `loadable` remains unknown and the status is
`UNVERIFIED` — the CLI says so instead of guessing.

## 4. Run a restore drill

The target command must load model and optimizer state and run training steps.

```bash
aevrel restore-drill \
  --checkpoint /path/to/target-checkpoint \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --steps 10 \
  --report ./reports/rank0.json
```

For a multi-rank checkpoint, run the drill once per rank and attach all reports:

```bash
aevrel readiness \
  --checkpoint /path/to/target-checkpoint \
  --target-world 3 \
  --drill-report ./reports
```

Incomplete rank coverage must remain non-`READY`.

## 5. Migrate a DCP checkpoint

```bash
aevrel migrate \
  --checkpoint /path/to/source.dcp \
  --target-world 3 \
  --output /path/to/target-3rank.dcp
```

The source is read-only. The target is staged and committed only after
verification. Re-running after an interruption resumes from the journal; do not
delete the intermediate work directory manually.

## 6. Rank-local target load proof

```bash
aevrel-target-load \
  --checkpoint /tmp/aevrel-demo-dcp/target-3.dcp \
  --rank 0 \
  --json
```

Plain mode prints a one-line proof (`target-load ok: mode=rank-local ...`).
The JSON form is the machine-readable evidence to attach to a drill report.

## 7. Kubernetes path

Build the CLI:

```bash
go build -o bin/aevrelctl ./cmd/ctl
export PATH="$PWD/bin:$PATH"
```

Without a kubeconfig:

```bash
aevrelctl migrate -f examples/k8s/migration.yaml --dry-run
```

With a reachable management cluster:

```bash
kubectl apply -f examples/checkpoint-control/checkpoint-record-selector.yaml
aevrelctl readiness training-step-100
```

See [Usage](USAGE.md) and [Target-ready](TARGET-READY.md) for the full
workflow.

## Reading the output

- `RESULT: OK` — the command did what it claims, with proof.
- `RESULT: NOT READY` with a reason and a `NEXT:` line — the checkpoint is
  not proven recoverable, and the next step is named.
- `RESULT: FAILED (<code>)` on stderr with exit code 2 — a hard failure,
  for example `aevrel migrate: DCP checkpoint directory not found: ...`.
- Unverified, stale, or incomplete states are never rendered as OK.

## Validation

```bash
python scripts/check_repo.py
python -m pytest -q
go test ./...
helm lint deploy/helm/aevrel
python scripts/first_run_acceptance.py
```

Multi-node and GPU checks are environment-specific. Record them as evidence
only when they actually run.
