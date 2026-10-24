# Kueue Preemption Guard

The optional Aevrel Kueue adapter protects training workloads with complete,
fresh, loadable checkpoint evidence from the configurable-preemption candidate
set.

The adapter does not replace Kueue. It does not change queue quotas, priority,
admission ordering, or Kueue's scheduler. It publishes integer labels consumed
by `PreemptionConfig.numericLabels`.

## Requirements

- Kueue with `ConfigurablePreemptions` enabled;
- a `PreemptionConfig` that filters on
  `aevrel.dev/preemption-protected`;
- Aevrel controller or a Job/Workload annotation publisher;
- explicit opt-in on the training Job or Workload.

## Publisher contract

An annotation-based publisher can expose:

```yaml
metadata:
  annotations:
    aevrel.dev/preemption-protection: "enabled"
    aevrel.dev/training-progress-percent: "85"
    aevrel.dev/checkpoint-complete: "1"
    aevrel.dev/checkpoint-version: "step-8500"
    aevrel.dev/checkpoint-age-seconds: "12"
```

`aevrel.dev/preemption-protection` is required. The adapter ignores objects
that do not opt in.

If a `CheckpointRecord` is attached, its readiness is authoritative. A record
is eligible only when it is `READY` with complete, fresh, loadable evidence.
Unknown, stale, or unverified records are protected by default.

## Published labels

```text
aevrel.dev/preemption-protected=1|0
aevrel.dev/preemption-reason=...
aevrel.dev/training-progress-percent=...
aevrel.dev/checkpoint-complete=1|0
aevrel.dev/preemption-decision-source=job|workload
```

The default policy is:

```text
protect_progress_gte = 0.8
require_checkpoint_complete = true
protect_unknown = true
```

The decision order is:

1. invalid or unknown state -> protected;
2. incomplete checkpoint -> protected;
3. progress at or above the threshold -> protected;
4. otherwise -> eligible.

## Install

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

Reference the generated profile from the ClusterQueue:

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

Explain a decision:

```bash
aevrelctl kueue status
aevrelctl kueue explain <workload>
```

## Strict protection

Configurable-preemption candidates are evaluated after Kueue's classical
preemption path. A strict claim is valid only when all of the following hold:

- `withinClusterQueue=Never`;
- `reclaimWithinCohort=Never`;
- `borrowWithinCohort.policy=Never`;
- fair sharing does not participate in the preemption decision;
- the ClusterQueue references the intended `PreemptionConfig`;
- the numeric-label rule uses `maxValue: 0`;
- the strict profile uses `fallbackValue: 1`.

Without these conditions, the adapter is an advisory filter, not an absolute
anti-preemption guarantee.

## Validation

```bash
go test -count=1 ./internal/kueueadapter
go test ./...
```

CPU-only Kueue tests validate label projection and candidate filtering. They do
not replace production-version, scale, or GPU validation.
