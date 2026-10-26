# Resume-state closure contract

A resume is only as trustworthy as the state it restores. This document
defines the **checkpoint-level resume-state manifest** (`schemaVersion: 1`)
and the read-only closure checker that classifies it. Both are
*scheduler-neutral*: the contract describes state, not the scheduler or
framework that produced it.

## Hard boundary: structure only, never loadability

The manifest records **which resume components exist**, who provides them,
their digests and their rank coverage. It is a declaration.

It never asserts that a checkpoint is loadable, that a resume will
succeed, or that training continuity holds. Those are separate,
evidence-backed claims produced by restore drills and readiness gates
(see `TARGET-READY.md` and `TARGET-LOAD.md`). A manifest that classifies
as `COMPLETE` means exactly one thing: *every required component is
declared, present and fully covered, with no contradicting declarations.*
Treating `COMPLETE` as "safe to resume" is a misuse of this contract.

## Components

Required set (authoritative, adapter-layer frozen):

| kind | meaning |
| --- | --- |
| `model` | model parameters |
| `optimizer` | optimizer state |
| `global_step` | consumed step counter |
| `dataloader` | dataloader position / cursor |
| `rng` | random number generator state |

Optional-but-must-declare set — the entry must exist even when the
component is absent, so consumers never have to guess:

| kind | meaning |
| --- | --- |
| `lr_scheduler` | learning-rate scheduler state |
| `sampler` | distributed sampler state |
| `rank_local` | per-rank local (non-sharded) state |

The manifest-level `topology` block (world size, mesh, placements) is
mandatory. Provider-defined component kinds are allowed to *exist*, but
they can never be marked `required` in the core closure: the core cannot
verify what it does not understand, so it refuses to vouch for it.

## Manifest shape

```json
{
  "schemaVersion": "1",
  "checkpoint": {
    "uid": "ckpt-0001",
    "version": "v1",
    "metadataDigest": "sha256:..."
  },
  "topology": {
    "worldSize": 2,
    "mesh": "1d",
    "placements": ["node-a/rank-0", "node-a/rank-1"]
  },
  "components": [
    {
      "kind": "model",
      "name": "model",
      "provider": "lrs.training",
      "digest": "sha256:...",
      "coverage": { "worldSize": 2, "ranks": [0, 1] },
      "required": true,
      "present": true
    }
  ]
}
```

Schema: `spec/v1/resume-state.schema.json`. Digests use the canonical
JSON encoding shared with the rank-local state manifest
(`aevrel.state.manifest.canonical_payload`), so the same bytes always
produce the same digest.

## How an adapter populates a manifest

`aevrel.lrs.resume_adapter.TrainingResumeAdapter` is the reference
adapter. It slices one frozen training-state snapshot into components and
serves each of them as its own bundle entry (`training.model`,
`training.optimizer`, `training.global_step`, `training.rng`,
`training.dataloader`, plus `training.lr_scheduler`, `training.sampler`
and `training.rank_local` when the snapshot carries them).

Its guarantees:

- **one snapshot** — the source state is captured exactly once per freeze,
  so every component belongs to the same instant;
- **one restore** — components are reassembled into a training state and
  applied through the kernel in a single call; components can never be
  written back individually, so a partial set can never be applied
  halfway;
- **fail closed** — a restore whose component set misses a required kind
  raises `RESUME_COMPONENT_MISSING` naming the kind;
- **no fabrication** — an optional component the snapshot does not carry
  is recorded as absent, never synthesized;
- **hooks** — state owned by the caller (for example a scheduler object
  outside the kernel) attaches as a hook component: its bytes are captured
  with the same snapshot and restored by that hook.

Because the adapter is rank-local, its manifest declares the ranks it
actually covered. A single-rank capture against `worldSize: 1` classifies
as `COMPLETE`; the same capture against `worldSize: 2` classifies as
`PARTIAL` with `RESUME_RANK_COVERAGE_INCOMPLETE`, which is the honest
answer until every rank contributes.

## Classification

`classify_resume_state(payload)` never raises for bad input and returns
one of four states:

| status | meaning |
| --- | --- |
| `COMPLETE` | every required component declared, present, fully covered; no contradictions |
| `PARTIAL` | at least one required component present, but at least one closure problem exists |
| `MISSING` | no required component is present at all — there is nothing to resume from |
| `MALFORMED` | the payload violates the schema and cannot be interpreted |

`parse_resume_state(payload)` is the strict path: it raises `ValueError`
on schema violations and duplicate component kinds.

Each failure carries a specific reason code with machine-readable
details (`kind`, `missingRanks`, expected/declared values, and so on). All
codes are catalogued, with what each one blocks, in
[FAILURE-CODES.md](FAILURE-CODES.md):

| code | raised when |
| --- | --- |
| `RESUME_COMPONENT_MISSING` | a required component is undeclared or declared absent; an optional component is undeclared |
| `RESUME_COMPONENT_INVALID` | a component contradicts itself (present without digest, absent with digest), duplicates another kind, or misdeclares `required` |
| `RESUME_COMPONENT_DIGEST_MISMATCH` | component or checkpoint digests disagree with the checkpoint record |
| `RESUME_RANK_COVERAGE_INCOMPLETE` | a component's declared ranks do not cover the topology world size |
| `RESUME_TOPOLOGY_INCOMPATIBLE` | component expectations or placements disagree with the manifest topology |
| `RESUME_STATE_UNVERIFIED` | closure was never evaluated, yet a consumer requires a verdict |
| `RESUME_MANIFEST_INVALID` | the manifest itself is structurally invalid (schema violation) |

## Fail-closed usage

```python
from aevrel.state import classify_resume_state

report = classify_resume_state(payload)
if not report.closed:
    raise report.to_error()   # LocalError carrying the first specific code
```

`report.closed` is the only positive signal. `report.to_error()` returns
`None` for closed manifests and a `LocalError` otherwise, so a consumer
cannot accidentally treat `PARTIAL` as ready.

## Worked example: model-only checkpoint

A checkpoint that saved only model weights (a common failure mode when a
training loop is interrupted between component writes) classifies as:

- `status`: `PARTIAL` — the model is present, so this is not `MISSING`
- `RESUME_COMPONENT_MISSING` with `kind: optimizer`
- `RESUME_COMPONENT_MISSING` with `kind: global_step`
- `RESUME_COMPONENT_MISSING` with `kind: dataloader`
- `RESUME_COMPONENT_MISSING` with `kind: rng`

Any consumer that requires a closed state must refuse to resume. This is
the behaviour community reports converge on: the failure must be loud and
specific, not a silent partial restore.

## Binding a proof to a manifest

A migration proof (proof schema version `2`) carries exactly one
`resume_manifest_digest` field, and that field is folded into the proof
`root`. A proof therefore states *which* state closure its bytes belong
to, and the binding cannot be edited after the fact without breaking the
root.

```bash
aevrel proof-build --spec spec.yaml --plan plan.json --target-root ./target \
  --resume-state ./resume-state.json --out proof.json

aevrel proof-verify --proof proof.json --target-root ./target \
  --resume-state ./resume-state.json
```

Rules:

- The field is required but nullable. `null` is an explicit statement that
  the proof makes no resume-closure claim; it is never an implicit default.
- `combine_proofs` refuses to merge rank proofs whose digests disagree, so
  a merged proof cannot straddle two closures.
- `verify_proof(..., resume_manifest_digest=...)` fails both when the proof
  is bound to a different manifest and when the proof is unbound.
- `TargetTaskExecutor(..., resume_manifest_digest=...)` (or
  `aevrel agent --resume-state`) is how a target agent stamps its rank
  proofs.

## End-to-end golden path

`tests/integration/test_resume_golden_path.py` runs the whole chain on one
deterministic CPU harness: train at world 4, freeze the state into a
persisted component bundle, classify the manifest (`COMPLETE`), restore one
world-3 rank **from the persisted bytes only**, verify every component
digest, then keep training.

Two claims are asserted, and both are falsifiable:

- **Batching continuity** - one sampler owns the global sample stream and
  its RNG is a resume component, so the resharded continuation replays the
  exact same global batches as a reference run that was never saved.
- **Weight equivalence** - the resharded continuation matches the reference
  trajectory to `<= 1e-12` (observed `0.0` in float64), because the only
  difference between the two is the order in which the gradient sum is
  accumulated. A negative control that continues the same stream from a
  cold model drifts by `~5e-2`, so the assertion cannot pass vacuously.

Fail-closed is covered from both sides:

- a bundle missing `dataloader` raises `RESUME_COMPONENT_MISSING` even when
  the manifest advertises a complete set, so a divergence between the
  commitment and the bytes is caught at restore time;
- a manifest whose closure is incomplete degrades readiness to
  `TARGET_BLOCKED` / `loadable: false` / `L3` under `--require-state-complete`.

Restoring never rewrites the source bundle: the test hashes the bundle tree
before and after and requires it to be byte identical.

## Not covered here

- Deep readiness semantics (freshness, movability) — see
  `READINESS-SEMANTICS.md`.
- How target-ready reports anchor the digest across ranks — see
  `TARGET-READY.md`.
- Framework-specific adapters (for example TorchTitan or Ray) — the
  manifest is framework-neutral; adapters populate it.
