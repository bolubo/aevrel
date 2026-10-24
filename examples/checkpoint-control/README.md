# Minimal checkpoint control-plane workflow

This example uses only Aevrel CRDs. It does not require Kueue, Volcano, or a
scheduler plugin.

## 1. Inspect a checkpoint

```bash
kubectl apply -f checkpoint-record-selector.yaml
kubectl get checkpointrecords
aevrelctl readiness training-step-100
```

`agentSelector` selects every ready agent pod and reads each agent's DCP
metadata and local shard facts in parallel. The controller merges the results
by rank. A repeated rank must describe the same shard; missing ranks,
out-of-range ranks, or conflicting facts make `complete=false`.

Without a selector, `spec.agentEndpoint` can still point at one agent.

## 2. Run a real restore drill

```bash
kubectl apply -f restore-drill.yaml
kubectl get restoredrills training-step-100 -w
aevrelctl readiness training-step-100
```

`RestoreDrill` creates a `batch/v1` Indexed Job with
`completions=parallelism=targetWorldSize` and injects the checkpoint path,
version, step count, and report path. Every rank must load the model and
optimizer, run at least N training steps, and write a report through
`aevrel-target-report` before `loadable=true` is published.

The controller never treats file existence or readable metadata as load proof.

## 3. Produce a rescue decision

```bash
kubectl apply -f rescue-decision.yaml
kubectl get rescuedecisions
aevrelctl readiness training-step-100
```

The same decision can be created from the CLI:

```bash
aevrelctl rescue \
  --event Preempted \
  --workload training \
  --checkpoint training-step-100 \
  --target cluster-b
```

Decision rules:

- complete, fresh, `loadable=true`, and backed by drill proof:
  - no target: `restore`
  - target present and `movable=true`: `migrate`
- missing checkpoint, no drill, failed drill, stale state, or an immovable
  target: `hold`
- `restart` is returned only when the user explicitly sets
  `fallback: restart` and the checkpoint cannot be recovered

A decision only writes `RescueDecision.status`. It never starts a second
training workload, so a control-plane misclassification cannot create a
split-brain workload.

## 4. Create decisions from Kubernetes events

Opt a training Job in with:

```yaml
metadata:
  labels:
    aevrel.dev/rescue: enabled
  annotations:
    aevrel.dev/checkpoint-record: training-step-100
```

Warning events such as `Evicted`, `Preempted`, `BackoffLimitExceeded`,
`FailedScheduling`, and `OOMKilled` create a `RescueDecision`. The event
watcher does not depend on a scheduler; Kueue or Volcano may provide richer
workload semantics when present.
