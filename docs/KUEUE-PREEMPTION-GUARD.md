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

The published `aevrel.dev/preemption-reason` is one of:

- `InvalidStateProtected` / `InvalidStateEligible` - the published fields could
  not be parsed;
- `UnknownStateProtected` / `UnknownStateEligible` - nothing was published;
- `CheckpointIncomplete` - a complete checkpoint is required and missing;
- `ProgressThreshold` - progress is at or above the threshold;
- `Eligible` - none of the above applies.

## Optional fact labels

The protection labels above are always published. The scheduler/runtime
contract facts are opt-in:

```bash
kueue-adapter --envelope-labels
```

They are transport, not decision input: the `protected` / `eligible` outcome is
identical with the flag on or off, and the policy never reads them. A fact set
that fails conformance publishes no labels at all, and any previously published
envelope is withdrawn in the same patch.

```text
aevrel.dev/schema-version=v1
aevrel.dev/v1-complete=1|0|unknown
aevrel.dev/v1-fresh=1|0|unknown
aevrel.dev/v1-loadable=1|0|unknown
aevrel.dev/v1-movable=1|0|unknown
aevrel.dev/v1-observed-at=20060102T150405Z
aevrel.dev/v1-fact-source=checkpoint-record|checkpoint-record-ambiguous|annotation-fallback|unknown
aevrel.dev/v1-reason-codes=...
aevrel.dev/v1-restore-mode=restore|restart|migrate|hold
aevrel.dev/v1-estimated-rto-seconds=<integer>|unknown
aevrel.dev/v1-envelope-conformance=valid
```

See [scheduler-runtime-contract-v1.md](scheduler-runtime-contract-v1.md) for the
field semantics, the label encoding rules, and the fail-closed matrix.

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
