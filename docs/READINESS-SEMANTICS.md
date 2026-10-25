# Readiness semantics v1 (L3 / L4 subset)

This document freezes the readiness semantics that Line 1 depends on:
what `complete`, `fresh`, `loadable`, `movable` and `state_complete` each
prove, how they combine into a status, and how the Python adapter and the
Go controller stay identical. It is the Line 1 subset of RFC #32; deeper
verification semantics (proof freshness, topologies, partial drills) come
later and are listed under "Not covered".

## Authority and drift control

The Go controller is authoritative. The Python adapter computes a local
convenience assessment with the same rules. Both implementations are
driven by **the same JSON fixtures**
(`tests/fixtures/readiness/conformance/*.json`), consumed by
`tests/unit/test_readiness_conformance.py` and
`internal/control/readiness_conformance_test.go`. A semantic change that
only lands on one side fails a test on that side.

## L3 and L4

| Claim | Meaning | Requires |
| --- | --- | --- |
| **L3** | The checkpoint can be loaded and training can step: model, optimizer and step counter are intact and a restore drill succeeded. | `complete` + `fresh` + drill evidence |
| **L4** | L3 plus proven resume-state continuity: dataloader position, RNG and sampler state are part of a complete closure. | L3 + resume-state closure `COMPLETE` |

L3 is the default claim. L4 is **opt-in**: a consumer must ask for it with
`require_state_complete` (CLI: `readiness --require-state-complete`).
Without the flag the result stays an L3 claim and merely *reports*
`state_complete`; it never calls itself L4.

### What each level does and does not prove

| Question | L3 | L4 |
| --- | --- | --- |
| Did the bytes arrive intact? | yes | yes |
| Do model weights, optimizer and step counter load? | yes | yes |
| Is the dataloader position continuous? | **not claimed** | yes, when `dataloader` is in the closure |
| Is the RNG stream continuous? | **not claimed** | yes, when `rng` is in the closure |
| Is the sampler state continuous? | **not claimed** | yes, when `sampler` is declared present |
| Is the closure that was drilled the closure being assessed? | **not claimed** | yes: both manifest digests must match |
| Does the trainer framework honour all of it? | **not claimed** | adapter-dependent, see [RESUME-STATE.md](RESUME-STATE.md) |

Neither level proves throughput, convergence, or that the resumed run ends
at the same loss. `tests/integration/test_resume_golden_path.py` shows the
trajectory replay for one deterministic CPU harness, not for every
framework; a framework adapter has to earn its own L4 by producing a
manifest with the components it can actually capture.

## state_complete is tri-state

`state_complete` reports the resume-state closure contract
([RESUME-STATE.md](RESUME-STATE.md)) without collapsing "unknown" into
"false":

| value | meaning |
| --- | --- |
| `null` | no closure evidence was supplied; nothing is claimed either way |
| `true` | closure is `COMPLETE` |
| `false` | closure is `PARTIAL`, `MISSING` or `MALFORMED` |

With `require_state_complete` enabled, `state_complete` must be `true` for
the result to stay `READY`; otherwise the result is blocked and carries
`STATE_CLOSURE_INCOMPLETE` with the specific closure codes in the detail.
A blocked gate also retracts `loadable`: the checkpoint did load, but not to
the contract that was requested, so the L3 fact is not upgraded into an L4
claim.
The blocked status is `TARGET_BLOCKED`: the checkpoint itself may be fine,
but the target contract (L4) is not satisfied.

## Manifest anchor

A closure verdict describes one specific manifest. A drill proof describes
whatever the target ranks actually restored. Both are only meaningful together
when they describe the *same* state, so `assess_readiness` compares the digest
the drill ranks report against `manifest.compute_digest()` of the assessed
manifest:

- equal: the verdict and the proof refer to the same state; `loadable` keeps
  its drill value and `last_drill_digest` records the binding;
- different: the result is `TARGET_BLOCKED` with
  `RESUME_COMPONENT_DIGEST_MISMATCH`, `loadable=false`, and the L4 claim is
  refused — a proof for another manifest cannot vouch for this one;
- absent: the drill carried no closure evidence (for example a legacy schema
  v1 report), so nothing is anchored and nothing is claimed. The assessment
  stays `L3` even when the closure itself is `COMPLETE`: a complete closure
  nobody resumed from is an L3 fact, not an L4 proof.

A second anchor compares the manifest with the record: `uid`, `version` and
`metadataDigest` must describe this checkpoint, otherwise the result is
`TARGET_BLOCKED` with `RESUME_MANIFEST_INVALID`.

Ranks that report different digests among themselves are rejected earlier, as
an invalid drill report: a drill that restored two different manifests did not
prove anything about either of them.

## Where closure evidence comes from

The control plane never guesses closure; it asks the agents that can see the
checkpoint storage. Checkpoint record protocol **version 2** adds one section
to the agent response:

```json
{
  "version": "2",
  "record": { "...": "checkpoint facts, unchanged" },
  "resumeState": {
    "closure": "COMPLETE",
    "manifestDigest": "sha256:...",
    "components": [{ "kind": "model", "required": true, "present": true }],
    "reasonCodes": [],
    "checkpoint": {
      "uid": "default/job42",
      "version": "step-100",
      "metadataDigest": "sha256:..."
    }
  }
}
```

`checkpoint` is the identity the manifest claims for itself. It is what makes
the manifest anchor checkable on the control-plane side: without it the Go
authority could only trust a `COMPLETE` verdict, and a complete closure for some
other checkpoint would silently vouch for this one. The field is additive — an
agent that omits it keeps working, and the anchor then simply has nothing to
compare, which is reported as "no anchor evidence" rather than "anchored".

Rules:

- `resumeState: null` means the agent has **no** closure evidence. The record
  then reports `stateComplete: null` and can never claim L4: absence of proof
  is not a verdict.
- The agent is pointed at one manifest (`aevrel agent --resume-state`). It
  never invents a manifest and never digests a directory it was not pointed
  at.
- A manifest that does not parse is reported as `closure: MALFORMED` with an
  empty digest, so a broken file stays visible instead of degrading into
  "no evidence".
- When several agents serve one record (`agentSelector`) they must agree on
  `closure` and `manifestDigest`. Disagreement drops the evidence and adds
  `RESUME_MISMATCH` to the record reasons instead of letting a caller pick a
  convenient verdict.
- The controller persists the verdict on the record
  (`resumeClosure`, `resumeManifestDigest`, `resumeReasonCodes`,
  `stateComplete`, `readinessLevel`) and feeds it back into the single
  `AssessReadiness` entry point, so `spec.requireStateComplete` gates the
  cluster exactly like the CLI flag does.
- A record reaches L4 only when the closure is `COMPLETE` **and** a restore
  drill recorded the same `manifestDigest` in `lastDrillDigest`.

## Evidence source and invalidation

A published fact is only as good as the evidence under it, as the component
that produces that evidence, and as the rule that withdraws it. Every field
therefore carries all three. A row that cannot name its producer is a field
nobody is accountable for, which is how a `null` silently becomes a `true`:

| Field | Produced by | Evidence required | What invalidates it |
| --- | --- | --- | --- |
| `complete` | the provider inventory scan (`dcp` / `lrs` scanner, written to `status.ranks`) | an inventory listing every expected rank and shard | a rank or shard absent from a later scan (`RANK_MISSING`, `SHARD_MISSING`, `SHARD_TRUNCATED`) |
| `fresh` | the checkpoint provider's content timestamp (`created_at`, else filesystem `latest_mtime`) | a **content timestamp** within `max_age` | content older than `max_age` (`STALE`); no timestamp at all |
| `loadable` | the restore drill (`RestoreDrill` → `lastDrill*` on the record), judged by `AssessReadiness` | a drill that succeeded, ran at least `MIN_DRILL_STEPS` training steps, **named every expected rank**, and was produced for **this** checkpoint's version and metadata digest | drill failed (`DRILL_FAILED`); step count below the minimum (`DRILL_STEPS_MISSING`); fewer ranks named than the inventory covers (`DRILL_RANK_COVERAGE_INCOMPLETE`); no ranks named, or ranks with no world to measure them against (`DRILL_RANK_COVERAGE_UNREPORTED`); record moved to another version or digest (`DRILL_PROOF_STALE`); manifest bound to another checkpoint (`RESUME_MANIFEST_INVALID`); drill restored a different manifest than the closure describes (`RESUME_COMPONENT_DIGEST_MISMATCH`); gate requested and closure incomplete (`STATE_CLOSURE_INCOMPLETE`) |
| `movable` | the control plane, from `complete` plus `spec.target.worldSize` | source `complete` and a positive target world size | source incomplete or target world size not positive (`TARGET_BLOCKED`) |
| `state_complete` | the resume-state manifest classifier (CLI `aevrel verify`, or the agent pointed at one manifest) | a closure verdict for one manifest, anchored to the manifest a drill restored | manifest describes another checkpoint (`RESUME_MANIFEST_INVALID`); no evidence at all (`null`, never `false`) |

Three consequences are deliberate:

- **A success flag is not evidence.** `succeeded=true` is the drill's own
  claim; the step count, the rank coverage and the checkpoint identity are
  what turn it into a fact. A drill that loaded the bytes and stopped
  (zero steps) or that covered half the world proves that those bytes load,
  not that training resumes, so `loadable` is withdrawn rather than rounded up.
- **A drill that says nothing has proved nothing.** Rank coverage is carried
  as the ranks the drill actually restored (`ranks_reported`) against the world
  it was launched for (`ranks_expected`), not as a pair of counters. When the
  drill names no rank, the verdict is `DRILL_RANK_COVERAGE_UNREPORTED` — not
  "0 of N", which would report a restore failure the evidence never showed.
  Silence has never been coverage.
- **Evidence can expire without the checkpoint changing.** Every drill fact is
  bound to `(version, metadata digest)`. When the record moves to a newer
  checkpoint, the old proof stops supporting the new one even though it was
  valid when written: that is `DRILL_PROOF_STALE`, which is a different
  statement from `DRILL_FAILED` and is reported as such.

`movable` is a **coarse filter, not a topology verdict**. `movable=true` means
the source is complete and the target world size is positive; it says nothing
about mesh, placement or format compatibility. When the target world size
differs from the source's rank count and no topology check was performed, the
assessment keeps `movable=true` but attaches `TARGET_TOPOLOGY_UNVERIFIED`, so a
consumer cannot read the coarse check as a migration guarantee.

Unknown is never reported as `false` and never as safe: `loadable` and
`movable` stay `null` until evidence exists.

## Freshness: content timestamp, not scan time

RFC #32 left the freshness basis open (step, file mtime, or controller
`observed_at`). v1 freezes it as follows:

1. **Precedence**: provider-declared `created_at` if the record carries
   one, otherwise the filesystem `latest_mtime`.
2. **Excluded**: `observed_at`. It is the *scan* time recorded when the
   inventory ran, so using it would report every checkpoint as fresh
   forever.
3. **Fallback caveat**: mtime is what filesystem and DCP providers can
   supply today, but copies, rsync, caches and some object stores rewrite
   it. A provider that can expose a writer timestamp must populate
   `created_at`; mtime then stops being consulted.
4. **No timestamp at all** means `fresh = false`, and the record is not
   silently treated as new.

`estimated_rto_seconds` reuses the same age, so it cannot drift from the
freshness decision.

## Status precedence

The first matching rule wins:

1. inventory incomplete → `INCOMPLETE`
2. not fresh → `STALE`
3. drill ran and failed → `TARGET_BLOCKED`
4. the drill succeeded but its evidence does not support the claim
   (`DRILL_STEPS_MISSING`, `DRILL_RANK_COVERAGE_INCOMPLETE`,
   `DRILL_PROOF_STALE`), or an anchor is broken (`RESUME_MANIFEST_INVALID`,
   `RESUME_COMPONENT_DIGEST_MISMATCH`) → `TARGET_BLOCKED`, and `loadable`
   is withdrawn to `false`
5. no drill evidence → `UNVERIFIED`
6. `movable` explicitly `false` → `TARGET_BLOCKED`
7. otherwise → `READY`

Then the L4 gate applies: if it was requested and the closure is not
complete while the status is still `READY`, the status becomes
`TARGET_BLOCKED`. The gate never upgrades a status; it can only block.

## Reason codes

Every code a readiness verdict can carry is catalogued exactly once, with
what it blocks, in [FAILURE-CODES.md](FAILURE-CODES.md). This page does not
repeat that table, so the two cannot drift; a test enforces that every
emitted code is documented and no documented code is unemitted.

Reasons are ordered and deduplicated on `(code, detail)`. A consumer must
not collapse them into a set: order is part of the conformance contract.

## Cross-language conformance

`observed_at` is a canonical instant: UTC, second precision, `Z` suffix
(`2026-10-25T00:00:00Z`). Both implementations emit exactly these bytes, so a
scan timestamp taken with a `+08:00` offset normalizes to the same string as
its `Z` equivalent. Numeric offsets are never emitted; `isoformat()`-style
`+00:00` output is a bug, not a variant.

Everything compared must match exactly:

| Field | Comparison |
| --- | --- |
| `status`, `complete`, `fresh` | exact |
| `loadable`, `movable`, `state_complete` | exact, including `null` vs `false` |
| `readiness_level` | exact (`L3` / `L4`) |
| `reasons` | exact, ordered codes |
| `observed_at` | byte-identical string |
| `estimated_rto_seconds`, `target_world_size` | exact, including `null` |
| `last_drill`, `last_drill_digest` | exact where present; the Go side's zero value equals Python's `null` |

The fixtures in `tests/fixtures/readiness/conformance/` are the single source
of truth: the Python adapter and the Go controller read the same JSON and both
assert every field above. A disagreement fails on one side or the other.

The harness has already caught two real drifts: for an invalid target world
size the Python adapter recorded both the "target world size must be positive"
and "source is incomplete or target is invalid" reasons while the Go
controller recorded only the second; and the L4 claim was reachable without
any proof that the drill restored the manifest being assessed. Both are fixed
on the controller side.

## Not covered

- Proof freshness and invalidation, multi-rank drill coverage, mesh and
  placement compatibility (later RFC #32 slices).
- Any claim about throughput or performance: readiness is not a benchmark.
- Any claim that arbitrary model state is preserved: only the components
  named in the resume-state contract are covered.
