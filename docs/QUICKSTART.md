# Quick Start

This guide runs the existing Aevrel DCP path. It does not require a GPU or a
Kubernetes cluster.

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
python -m aevrel demo --dcp \
  --work-dir /tmp/aevrel-demo \
  --json
```

Success includes:

```text
verification.passed = true
dcp.source_readiness.status = READY
dcp.target_readiness.status = READY
dcp.target_load = ok
```

The demo creates a source DCP checkpoint, migrates it to a different world
size, loads the target, and verifies that the source remains unchanged.

## 3. Inspect a checkpoint

```bash
python -m aevrel readiness \
  --checkpoint /path/to/checkpoint \
  --target-world 3 \
  --json
```

`complete=true` and `fresh=true` only describe inventory. Without a real
validation report, `loadable` remains unknown and the status is `UNVERIFIED`.

## 4. Run a restore drill

The target command must load model and optimizer state and run training steps.

```bash
python -m aevrel restore-drill \
  --checkpoint /path/to/target-checkpoint \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --steps 10 \
  --report ./reports/rank0.json
```

For a multi-rank checkpoint, run the drill once per rank and attach all reports:

```bash
python -m aevrel readiness \
  --checkpoint /path/to/target-checkpoint \
  --target-world 3 \
  --drill-report ./reports
```

Incomplete rank coverage must remain non-`READY`.

## 5. Migrate a DCP checkpoint

```bash
python -m aevrel migrate \
  --checkpoint /path/to/source.dcp \
  --target-world 3 \
  --output /path/to/target-3rank.dcp
```

The source is read-only. The target is staged and committed only after
verification. Re-running after an interruption resumes from the journal; do not
delete the intermediate work directory manually.

## 6. Kubernetes path

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
