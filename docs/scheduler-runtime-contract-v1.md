# Scheduler / runtime contract v1

This document freezes the shape of the facts Aevrel publishes to schedulers.
It is the contract behind `aevrelctl kueue explain`, the optional envelope
labels on Kueue `Workload` objects, and every future adapter.

Aevrel is a checkpoint-facts provider. It does not replace Kueue, Volcano, or
Kubeflow, and it does not define fairness, quota, priority, or preemption
formulas. The contract below only says which facts exist, what they mean, and
when they must not be believed.

Related: [readiness semantics](READINESS-SEMANTICS.md),
[resume-state closure](RESUME-STATE.md),
[Kueue preemption guard](KUEUE-PREEMPTION-GUARD.md),
[failure code catalog](FAILURE-CODES.md).

## A. Envelope model

The envelope is a versioned, orthogonal set of facts.

1. **One versioned namespace.** Every fact carries the same
   `schemaVersion`, `aevrel.dev/v1`. A consumer confirms the version once; a new
   fact never forces a renegotiation of the facts that already exist.
2. **Orthogonal facts.** Each fact is independently readable. A consumer that
   only cares about `loadable` reads one value and ignores the rest. No fact is
   defined in terms of another consumer's vocabulary.
3. **Forward compatible.** Adding a fact adds one field or one label. Unknown
   fields and labels are ignored by existing consumers, and the meaning of an
   existing field never changes inside a major version.

### Facts are transport, not truth

`CheckpointRecord.Status.Readiness` is the only authoritative source. The
envelope is a projection of it:

- every fact can be recomputed from the record, at any time;
- no label may be edited to change a fact, because the next reconcile
  overwrites it from the record;
- Aevrel never becomes the second source of truth for a field a scheduler
  might grow natively later.

### Fact sources

`source` says what the facts came from. It is part of the fact set, not a side
channel.

| `source` | Meaning | Facts it may carry |
| --- | --- | --- |
| `checkpoint-record` | one authoritative `CheckpointRecord` was selected | all of them |
| `checkpoint-record-ambiguous` | several records match and none is unambiguously latest | none |
| `annotation-fallback` | the workload opted in, published annotations, but has no record | none |
| `unknown` | the workload opted in and published nothing | none |

Annotation values never become facts. A label is only ever a projection of
authoritative readiness, so a published progress percentage cannot be promoted
into `complete` or `fresh`.

## B. Field semantics

JSON names are used by `aevrelctl kueue explain --json`. `null` means unknown;
it never means "fine".

| Field | Type | Meaning | Invariant |
| --- | --- | --- | --- |
| `schemaVersion` | string | always `aevrel.dev/v1` | required |
| `status` | string | readiness status verbatim (`READY`, `UNVERIFIED`, `STALE`, `INCOMPLETE`, `TARGET_BLOCKED`, `UNKNOWN`) | copied, never derived |
| `complete` | bool or null | the saved inventory is complete | copied from `readiness.complete` |
| `fresh` | bool or null | the content is inside the freshness window | control plane decides; the envelope may only downgrade |
| `loadable` | bool or null | a restore drill proved the bytes load | `null` = no verdict |
| `movable` | bool or null | the checkpoint can move to the requested target | `null` = not evaluated |
| `observedAt` | string | RFC3339 time the evidence was collected | required for record facts |
| `source` | string | fact source, see above | required |
| `reasonCodes` | []string | readiness reason codes, verbatim and in order | must match the readiness layer 1:1 |
| `envelopeReasons` | []string | reasons that describe the envelope itself | never overlaps `reasonCodes` |
| `restoreMode` | string | `restore`, `restart`, `migrate`, or `hold`, from `control.DecideRescue` | advisory, never a command |
| `restoreReasonCode` | string | stable code for the advisory action | copied from the decision |
| `restoreReason` | string | human sentence for the advisory action | copied from the decision |
| `estimatedRTOSeconds` | int or null | expected recovery time, whole seconds | never `0`; `null` = unknown |
| `selectedRecord` | string | the record the facts came from | omitted when nothing was selected |
| `candidates` | int | how many records matched | `0` when none |
| `ambiguous` | bool | the selection could not be resolved | true forbids publication |
| `targetWorldSize` | int or null | world size the checkpoint targets | copied |
| `lastDrill` | string | reference to the drill that proved loadability | required for `restore` |
| `lastDrillVersion` | string | checkpoint version the drill used | copied |
| `lastDrillDigest` | string | manifest digest the drill bound | copied |
| `stateComplete` | bool or null | resume-state closure verdict | copied |
| `readinessLevel` | string | `L3` (bytes verified) or `L4` (closure verified) | copied |
| `conformance` | object | `{valid, violations}` for this fact set | see section E |

### Label encoding

Kueue only understands labels, so each fact also has a label form. Labels are
written only when the fact set passed conformance.

The namespace is `aevrel.dev/v1/*`. A Kubernetes label key cannot contain `/`
after the prefix, so the namespace is realised as the `v1-` leader on every fact
name: `aevrel.dev/v1-complete` is the label form of `aevrel.dev/v1/complete`.
`aevrel.dev/schema-version=v1` names the namespace those facts live in.

| Label | Value | Note |
| --- | --- | --- |
| `aevrel.dev/schema-version` | `v1` | names the namespace; the `aevrel.dev/` key prefix carries the domain |
| `aevrel.dev/v1-complete` | `1`, `0`, `unknown` | |
| `aevrel.dev/v1-fresh` | `1`, `0`, `unknown` | |
| `aevrel.dev/v1-loadable` | `1`, `0`, `unknown` | |
| `aevrel.dev/v1-movable` | `1`, `0`, `unknown` | |
| `aevrel.dev/v1-observed-at` | `20060102T150405Z` | compact UTC, because `:` is not legal in a label value |
| `aevrel.dev/v1-fact-source` | fact source | |
| `aevrel.dev/v1-reason-codes` | codes joined by `_`, or `unknown` | omitted when empty; `unknown` when the joined form exceeds the 63-character label limit |
| `aevrel.dev/v1-restore-mode` | advisory action | |
| `aevrel.dev/v1-estimated-rto-seconds` | integer seconds or `unknown` | never `0` |
| `aevrel.dev/v1-envelope-conformance` | `valid` | present only on a published envelope |

`unknown` is a first-class value. A missing number is never published as `0`,
because a consumer reading `0` would conclude "measured, and it is zero".

## C. Fail-closed matrix

Every way the envelope can be missing or unusable resolves to the same
conservative answer: no fact labels, protection unchanged, `restoreMode` stays
`hold` unless a positive verdict exists.

| Situation | Fact set | Envelope labels | Binary protection |
| --- | --- | --- | --- |
| no `CheckpointRecord`, no annotations | `source=unknown`, reason `NoCheckpointRecord` | published, all values `unknown` | unchanged |
| no `CheckpointRecord`, annotations present | `source=annotation-fallback` | published, no values | unchanged |
| annotations malformed | `source=annotation-fallback`, reason `AnnotationInvalid` | published, no values | unchanged, `InvalidStateProtected` |
| several records, no latest | `source=checkpoint-record-ambiguous`, `ambiguous=true` | withdrawn | protected, `InvalidStateProtected` |
| observation older than the configured age | `fresh=false`, reason `StaleObservation` | published with `fresh=0` | unchanged |
| fact set fails conformance | any | withdrawn | unchanged |
| adapter cannot read Kubernetes | none | untouched | not evaluated, never panics |

"Withdrawn" means every previously published envelope label is deleted in the
same patch. A consumer can therefore trust that a present envelope is current:
an absent one means "no usable facts now", never "the old facts still hold".

## D. Consumer mappings

Aevrel publishes facts. Each consumer restates them in its own vocabulary; the
translator lives next to the consumer, not in the fact layer.

### Kueue

Kueue is the first implemented consumer. It selects candidates with
`PreemptionConfig.numericLabels`, so the translator exposes integer labels and
a qualitative cost, and nothing else.

| Aevrel fact | Kueue-facing reading |
| --- | --- |
| `complete`, `fresh`, `loadable`, `movable` | numeric labels `1` / `0`; unknown values are omitted, never written as `0` |
| `restoreMode=restore` or `migrate` | preemption cost `resume` |
| `restoreMode=restart` | preemption cost `restart` |
| `restoreMode=hold` or invalid | preemption cost `unknown` |
| `estimatedRTOSeconds` | passed through as an integer, `unknown` when null |
| conformance failure | no numeric labels at all |

Aevrel does not write `priorityClass`, and it does not publish a preemption
cost formula. Admission still follows the binary protection label.

Envelope labels are opt-in: `kueue-adapter --envelope-labels`. With the flag
off, behaviour is byte-for-byte the pre-contract adapter.

### Volcano

Reserved. Not implemented. The translator reports `supported=false` and
publishes no fields. Aevrel does not claim Volcano support.

### Kubeflow

Reserved. Not implemented, same rule as Volcano. Kubeflow consumes training
state through its own APIs; a translator would be a separate project.

### Version compatibility

| Consumer | Contract version | Status |
| --- | --- | --- |
| `aevrelctl kueue explain --json` | v1 | implemented |
| Kueue envelope labels | v1 | implemented, opt-in |
| Kueue generic translator | v1 | implemented |
| Volcano | v1 | reserved, unsupported |
| Kubeflow | v1 | reserved, unsupported |

Adding a fact keeps the version. Changing the meaning of a field, removing a
field, or making `unknown` mean something else requires `v2` and a parallel
publication window.

## E. Conformance gate

Before a fact set is emitted, Aevrel checks the signal itself. It does not
predict what a consumer will do with it; it checks that the consumer cannot be
misled.

### (a) Internal consistency

| Rule | Violation |
| --- | --- |
| `fresh=true` requires a loadability verdict (`loadable` is not null) | `FreshWithoutLoadabilityVerdict` |
| `restoreMode=restore` requires a drill proof (`lastDrill` is not empty) | `RestoreWithoutDrillProof` |
| `estimatedRTOSeconds` is not null only when `complete=true` | `RTOWithoutCompleteInventory` |

A fresh checkpoint with no drill verdict is not schedulable evidence: freshness
alone cannot tell a scheduler whether the bytes will load. Such a set is
invalid, so no envelope is published, and the binary protection label remains
the only signal. This is the intended consequence, not an oversight.

### (b) Self-description

| Rule | Violation |
| --- | --- |
| `schemaVersion` is exactly `aevrel.dev/v1` | `SchemaVersionMismatch` |
| `source` is not empty | `MissingSource` |
| `source` is a known value | `UnknownFactSource` |
| record facts carry `observedAt` | `MissingObservedAt` |

### (c) No fabrication

| Rule | Violation |
| --- | --- |
| a fallback source carries no verified values and no drill proof | `FallbackClaimsVerifiedFacts` |
| a fallback source reports `hold` | `FallbackClaimsRestoreMode` |
| an unknown source carries no values at all | `UnknownSourceClaimsFacts` |
| `estimatedRTOSeconds`, when present, is greater than zero | `RTOMustBePositiveOrNull` |
| an ambiguous selection is never published | `AmbiguousRecordSelection` |
| a value that does not fit the transport is `unknown`, never truncated | no code; the source list stays authoritative on the record |

Any violation makes the whole set invalid. There is no partial publication and
no silent repair: a violation is always reported with its code, in
`conformance.violations` and in `aevrelctl kueue explain`.

## F. Invariants and their failure semantics

| Invariant | If it would be violated |
| --- | --- |
| the record is the only source of truth | the fact set is rebuilt from the record; a hand-edited label is overwritten |
| facts never overstate safety | the set is invalid and nothing is published |
| unknown stays unknown | the value is `null` / `unknown`, never a default |
| an advisory action is not a command | `restoreMode` is documented as advisory; Aevrel never starts, stops, or migrates a workload |
| labels are not decision input | `Policy.Decide` never reads them; changing an envelope cannot change a protection decision |
| the binary decision is stable | the `protected` / `eligible` outcome is unchanged by the contract |

## G. Relation to the readiness and verification layer

The readiness layer decides what is true. The envelope only carries it.

- every readiness reason code appears in `reasonCodes` unchanged, in order, so
  a consumer can map the two layers 1:1;
- when the readiness layer says a fact is unknown, the envelope publishes
  `unknown`; it never upgrades a missing verdict into a safe default;
- fail-closed discipline is inherited, not re-implemented: an unknown
  `loadable` cannot become `false` "for safety", because `false` is itself a
  claim;
- a fact that the readiness layer does not define cannot be invented by the
  envelope.

## H. Open questions this version does not answer

Recorded so they are not mistaken for settled behaviour:

- whether a consumer may use `estimatedRTOSeconds` as a scheduling weight
  (that is consumer policy, and Aevrel publishes no weight);
- whether Kueue will grow native checkpoint fields, which would retire some
  labels;
- how a multi-cluster target should appear in `movable` and `targetWorldSize`.

None of these changes the facts above.
