# Failure code catalog

Every readiness verdict an Aevrel surface reports is explained by a code.
This page is the single place that says what each one means and what it
blocks. `tests/unit/test_failure_code_catalog.py` keeps it honest: the
catalog and the enums must describe exactly the same set, so a code cannot
be added to the code base without a documented meaning.

Reading the tables: **Blocks** names what the code withholds. A code never
says "maybe"; it either withholds loadability (`loadable: false`), withholds
movability (`movable: false`), withholds READY, or withholds the L4 claim.

## Physical inventory

These come from comparing what the storage has with what the record expects.

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `METADATA_MISSING` | the provider found no checkpoint metadata at all | everything above `INCOMPLETE` |
| `SOURCE_UNREADABLE` | an agent could not read the source path or its digest is empty | READY (evidence cannot be trusted) |
| `RANK_MISSING` | an expected rank was never observed | READY; the record is `INCOMPLETE` |
| `RANK_DUPLICATE` | two observations claim the same rank | READY (ownership is ambiguous) |
| `RANK_UNEXPECTED` | an observation reports a rank outside `sourceWorldSize` | READY (the record and the storage disagree) |
| `SHARD_MISSING` | a rank is present but has no shards | READY |
| `SHARD_TRUNCATED` | a shard is shorter than its declared length | READY (the bytes are not what was written) |
| `OBSERVATION_INVALID` | an agent response was structurally unusable | READY |

## Freshness

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `STALE` | the content timestamp is older than `maxAgeSeconds` | READY; status becomes `STALE` |

## Restore drills

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `DRILL_MISSING` | no restore drill has verified this checkpoint | `loadable` stays `null` |
| `DRILL_FAILED` | the drill ran and failed | `loadable: false` |
| `DRILL_STEPS_MISSING` | the drill reports success but ran fewer than `MIN_DRILL_STEPS` training steps | `loadable: false` — loading bytes is not resuming training |
| `DRILL_RANK_COVERAGE_INCOMPLETE` | the drill named ranks, but fewer than the checkpoint's inventory covers: the missing ranks are listed in the detail | `loadable: false` — a partial restore is not a restorable checkpoint |
| `DRILL_RANK_COVERAGE_UNREPORTED` | the drill named **no** rank, or named ranks without a world size to measure them against | `loadable: false` — silence is not coverage, and it is not "0 of N" either |
| `DRILL_PROOF_STALE` | the proof is bound to a version or metadata digest the record no longer has | `loadable: false` — the proof describes a checkpoint that is gone, which is not the same as a broken drill |

The last four withdraw a claim the drill itself reported as successful. They
exist because `succeeded=true` is the drill's assertion, not evidence: only the
step count, the rank coverage and the checkpoint identity turn it into a fact.

`DRILL_RANK_COVERAGE_UNREPORTED` is deliberately separate from
`DRILL_RANK_COVERAGE_INCOMPLETE`. "The drill proved 2 of 4 ranks" and "the
drill told us nothing" are different facts about the world, and an operator
acts differently on each; collapsing the second into the first would report a
failure the evidence never showed.

## Target readiness

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `TARGET_UNSPECIFIED` | no target world size or mesh was given | `movable` stays `null` |
| `TARGET_BLOCKED` | a required target-side condition failed, including the closure gate | `loadable: false` for this target |
| `TARGET_TOPOLOGY_UNVERIFIED` | `movable` is true and the target world size differs from the source's rank count, with no topology check | nothing — it is a caveat on `movable`, which is a coarse filter and not a topology verdict |

## Resume-state closure

Produced by `classify_resume_state` on a manifest. These describe the
*structure* of the saved state; none of them claims loadability. The exact
predicate behind each code (what precisely makes a component invalid) lives
in [RESUME-STATE.md](RESUME-STATE.md); this table is the index, that page is
the definition.

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `RESUME_COMPONENT_MISSING` | a required component (`model`, `optimizer`, `global_step`, `dataloader`, `rng`) is absent | closure `COMPLETE` |
| `RESUME_COMPONENT_INVALID` | a component is present but its digest or name is unusable | closure `COMPLETE` |
| `RESUME_COMPONENT_DIGEST_MISMATCH` | a component digest disagrees with the manifest, or a drill restored a different manifest | closure `COMPLETE`; also withdraws `loadable` on the readiness side |
| `RESUME_RANK_COVERAGE_INCOMPLETE` | component coverage does not span the declared world size | closure `COMPLETE` |
| `RESUME_TOPOLOGY_INCOMPATIBLE` | the manifest topology contradicts the declared mesh or placements | closure `COMPLETE` |
| `RESUME_STATE_UNVERIFIED` | a consumer demanded a closure verdict from a manifest that was never evaluated | the L4 claim |
| `RESUME_MANIFEST_INVALID` | the manifest is structurally invalid, or its identity disagrees with the checkpoint record | the L4 claim; status becomes `MALFORMED` or `TARGET_BLOCKED` |

## Readiness anchoring

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `STATE_CLOSURE_INCOMPLETE` | `require_state_complete` is set and the closure is not `COMPLETE` | READY for that target; the level stays `L3` |

## Rescue advisory

Emitted by `control.DecideRescue` and published as `restoreMode` /
`restoreReasonCode` in the scheduler envelope. A code here explains the
recommendation, not an outcome: Aevrel never restarts or migrates a workload
itself. See
[scheduler-runtime-contract-v1.md](scheduler-runtime-contract-v1.md).

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `RESTORE_READY` | a fresh checkpoint passed a restore drill and no target was requested | nothing; this is the only positive `restore` verdict |
| `MIGRATE_READY` | the checkpoint passed a drill and is movable to the requested target | nothing; this is the only positive `migrate` verdict |
| `RESTART_FALLBACK` | the readiness was unusable and the caller opted into a restart fallback | `restore`; the action becomes `restart` |
| `CHECKPOINT_NOT_FOUND` | no `CheckpointRecord` is associated with the workload | every restore claim |
| `CHECKPOINT_INCOMPLETE` | the checkpoint inventory is not complete | every restore claim |
| `CHECKPOINT_STALE` | the content is older than the freshness window | every restore claim |
| `PROOF_MISSING` | `loadable` is set without a restore-drill reference | `restore` (the evidence is unbound) |
| `TARGET_UNKNOWN` | a target was requested and mobility to it is unknown | `migrate` |

## Control-plane observation

Emitted by the controller while merging what agents reported.

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `AGENT_UNREACHABLE` | no agent answered, or answered with an unusable protocol version | READY; status becomes `UNKNOWN` |
| `METADATA_MISMATCH` | two agents report different metadata digests | READY |
| `PROVIDER_MISMATCH` | two agents report different providers | READY |
| `FORMAT_VERSION_MISMATCH` | two agents report different format versions | READY |
| `VERSION_MISMATCH` | an agent reports a version other than the record's | READY |
| `RESUME_MISMATCH` | two agents report different resume-state closures or manifest digests | the L4 claim (the evidence is dropped) |

## CLI-only failures

The readiness command also exits `2` on inputs that never become a verdict.
`RESUME_MANIFEST_INVALID` doubles as its "the flag needs a manifest" error,
and `DRILL_REPORT_INVALID` reports drill reports from different ranks that
disagree about the manifest digest. Both are input errors: nothing was
assessed, so no verdict is claimed.

## Target-ready conditions are not codes

`TargetReadyStatus.Condition` uses a separate enum (`Ready`, `Incomplete`,
`LoadFailed`, `StepFailed`, `VersionMismatch`, `StateClosureIncomplete`,
`InvalidReport`, `InvalidJob`, `Unsupported`, `Timeout`, `JobFailed`,
`ProofPublishFailed`, `Pending`). A condition is the *shape* of the answer;
the reason codes above are the explanation. See
[TARGET-READY.md](TARGET-READY.md) for how they combine.

## Scheduler envelope

Emitted by the Kueue adapter when it projects a fact set onto
`aevrel.dev/*` labels. A code here means the envelope was withheld; it never
means the workload is safe. The binary protection decision is unaffected.
See [scheduler-runtime-contract-v1.md](scheduler-runtime-contract-v1.md) for the
conformance gate itself.

These identifiers are CamelCase because they describe an envelope-level
projection rather than a readiness verdict. They are enforced by the
`internal/kueueadapter` tests and by
`tests/acceptance/verify_line2_acceptance.py`.

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `FreshWithoutLoadabilityVerdict` | `fresh` is true while `loadable` is null | the whole envelope (`fresh` alone is not schedulable evidence) |
| `RestoreWithoutDrillProof` | `restoreMode=restore` with no `lastDrill` reference | the whole envelope |
| `RTOWithoutCompleteInventory` | `estimatedRTOSeconds` is set while `complete` is false | the whole envelope |
| `RTOMustBePositiveOrNull` | `estimatedRTOSeconds` is zero or negative | the whole envelope (0 is not a known RTO) |
| `SchemaVersionMismatch` | the fact set claims a version other than `aevrel.dev/v1` | the whole envelope |
| `MissingSource` | the fact set has no `source` | the whole envelope |
| `UnknownFactSource` | `source` is not one of the four defined values | the whole envelope |
| `MissingObservedAt` | record facts carry no `observedAt` | the whole envelope |
| `FallbackClaimsVerifiedFacts` | an annotation fallback carries verified values | the whole envelope |
| `FallbackClaimsRestoreMode` | an annotation fallback claims anything but `hold` | the whole envelope |
| `UnknownSourceClaimsFacts` | the unknown source carries any value | the whole envelope |
| `AmbiguousRecordSelection` | more than one record matches and none is latest | the whole envelope |

## Scheduler envelope provenance

Emitted alongside a fact set in `envelopeReasons`. These describe the envelope,
never the checkpoint, and they only ever downgrade.

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `NoCheckpointRecord` | the workload opted in and published nothing | every fact value |
| `AmbiguousCheckpointRecords` | several records match and none is unambiguously latest | every fact value |
| `AnnotationFallback` | annotations exist but no record does | every fact value |
| `AnnotationInvalid` | the annotations exist but could not be parsed | every fact value |
| `StaleObservation` | `observedAt` is older than the configured maximum age | the `fresh` verdict |
