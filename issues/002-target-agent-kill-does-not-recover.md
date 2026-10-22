# Target agent kill during transfer does not recover

Status: resolved
Severity: P1
Area: controller, target agent, e2e
Source: kind G3 smoke, 2026-10-22

## Expected

If a target agent is killed after work starts and restarted with the same
state directory, the controller should recover the affected tasks and either
reach `succeeded` or a repairable `commit-incomplete` state.

## Actual

With `e2e/run.py --scenario target-kill`, target-0 is stopped while tasks are
in flight and restarted on the same port/state directory. The migration ends:

- CR phase: `failed`
- counts: `failed=4, prepared=4, staged=0`
- target-0 restart log shows the agent listening, but no recovery to success

No committed target state was produced.

## Reproduction

```bash
python3.12 e2e/run.py \
  --scenario target-kill \
  --cluster-name shardferry-e2e \
  --host-ip <wsl-eth0-ip> \
  --keep
```

The harness waits for `preparing`/`transferring`, kills target-0, restarts it,
then waits for `succeeded`.

## Suspected code

- target task persistence/restart behavior in `src/shardferry/agent_state.py`
- `internal/execution/runner.go` retry and task reattachment
- controller task reconciliation after an agent connection failure

## Acceptance

Add a regression that:

1. kills a target agent during transfer;
2. restarts it with the same state dir/listen address;
3. verifies the affected ranks reattach and the migration reaches
   `succeeded`, or enters a documented repairable state with all target
   bytes still protected by the commit barrier.

## Resolution (2026-10-22)

The target task state machine now permits `transferring -> prepared` so a
restarted target agent can be prepared again from its durable task state.
`e2e/run.py --scenario target-kill` restarts target-0 on the same state
directory and reaches `succeeded` with 8/8 committed ranks and a report
artifact.

Changed files:

- `src/shardferry/agent.py`
- `src/shardferry/agent_state.py`

Verified with:

- `e2e/run.py --scenario target-kill` (16→8, success)
- `pytest -q` (204 passed, 2 skipped)
