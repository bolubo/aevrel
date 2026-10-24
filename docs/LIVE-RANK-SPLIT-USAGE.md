# Live Rank Split Workflow

Live Rank Split (LRS) is an experimental path for moving individual training
ranks while the remaining ranks continue.

Use it only when:

- the source training job is still running;
- only selected ranks move;
- source and target do not share a checkpoint directory;
- the logical world size remains unchanged;
- the workflow is being evaluated as experimental.

For a stopped source, use a static `aevrelctl migrate` workflow instead.

## Configuration

```yaml
spec:
  mode: live-rank-split
  source:
    worldSize: 4
  target:
    worldSize: 4
    validation:
      version: step-100000
      steps: 10
  live:
    maxInFlight: 2
    stateMode: L4
    stateRoot: /state/step-100000/rank{rank}
    stateProviders:
      - python.random
      - numpy.random
      - torch.cpu.rng
      - training.state
    handoffEndpoint: http://lrs-handoff.live.svc:9460
    cutover:
      quorum: 3
      requireStateComplete: true
```

`maxInFlight` controls how many ranks may move concurrently. It must be at
least 1 and less than the source world size. Use
`--max-in-flight N` to override the YAML for a command.

## User flow

Preflight:

```bash
aevrelctl preflight -f live-rank-split.yaml --json
```

Preview:

```bash
aevrelctl plan -f live-rank-split.yaml
```

Validate the generated resource without creating it:

```bash
aevrelctl migrate -f live-rank-split.yaml --dry-run
```

Run and observe:

```bash
aevrelctl migrate -f live-rank-split.yaml --wait
```

The worker is optional at the Helm level:

```bash
aevrel-live-worker \
  --listen 0.0.0.0:9460 \
  --state-dir /var/lib/aevrel/live-worker
```

Enable it through Helm with `liveWorker.enabled=true`, or point
`controller.liveURL` at an external worker.

Inspect rank-level state:

```bash
aevrelctl describe migration live-rank-split-demo --ranks
```

Success requires:

- no rank remains in flight;
- coverage includes every logical rank;
- quorum is satisfied;
- L4 runs report `stateComplete=true`;
- target validation is ready;
- proof is readable.

## Failures

| Condition | Meaning | Next step |
|---|---|---|
| `LiveExecutorUnavailable` | controller has no live URL or the executor is unreachable | configure `controller.liveURL` |
| `LiveObservationInvalid` | executor returned invalid phase, rank, coverage, or quorum | inspect the executor; do not edit status manually |
| `target-failed` | data may be present but target load/steps failed | inspect `describe --ranks` and the target Job |
| `cancelled` | the user cancelled | confirm whether target data was committed |

The controller fails closed when the live executor is unavailable. It does not
silently fall back to a static migration.

## Current limitations

- The Go controller and CPU-contract worker are available.
- GPU collective continuity and real multi-node takeover remain unvalidated.
- CPU 4-rank tests use a staged destroy/reinitialize flow, not the final live
  collective behavior.
- Do not claim `global_stop=0` or `steps_lost=0` for GPU without a real test.
