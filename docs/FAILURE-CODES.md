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
| `DRILL_FAILED` | the drill ran and failed, or its evidence is for another version or digest | `loadable: false` |

## Target readiness

| Code | Emitted when | Blocks |
| --- | --- | --- |
| `TARGET_UNSPECIFIED` | no target world size or mesh was given | `movable` stays `null` |
| `TARGET_BLOCKED` | a required target-side condition failed, including the closure gate | `loadable: false` for this target |

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
