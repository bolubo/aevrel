# Transient target execute failure does not converge

Status: open
Severity: P1
Area: controller, execution runner, e2e
Source: kind G3 smoke, 2026-10-22

## Expected

A single transient target execute failure should be retried within
`policy.maxAttemptsPerRank`. The migration should either:

- recover and reach `succeeded` with all ranks committed, or
- fail consistently with no committed target state.

It must not leave the CR failed while the target task has already committed.

## Actual

With `e2e/run.py --scenario target-fail`, rank 0 fails once during execute:

- CR phase: `failed`
- counts: `failed=1, prepared=7, staged=0`
- target task rank 0: `state=committed`, `report.status=success`
- execution snapshot still reports rank 0 as `failed`

The controller and the target task disagree about whether the rank completed.

## Reproduction

Use a Python 3.12 environment for the e2e agents (WSL Python 3.14 has a local
HTTP server defect), and a WSL eth0 IP rather than `127.0.0.1`:

```bash
python3.12 e2e/run.py \
  --scenario target-fail \
  --cluster-name shardferry-e2e \
  --host-ip <wsl-eth0-ip> \
  --keep
```

Inspect:

```bash
kubectl get checkpointmigration <name> -o yaml
find <work-dir>/agents/target-state-0/tasks -type f
```

## Suspected code

- `internal/execution/runner.go` retry / `PhaseFailed` transition
- `internal/controller/reconciler.go` Run status refresh
- `internal/controller/preparer.go` execution Run construction
- `src/shardferry/agent.py` target task state transitions

## Acceptance

Add a regression that injects one execute failure and asserts one of:

1. retry reaches `succeeded` and all target files are committed;
2. terminal failure leaves no target task committed and the error is
   reported consistently in the CR and execution snapshot.
