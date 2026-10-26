# Resume-state closure: implementation and upgrade notes

This note explains what the resume-state closure change delivers, the breaking
contract changes it introduces, and how to verify it. The contract itself is
specified in [Resume-state closure contract](RESUME-STATE.md); the promise
boundary between readiness levels is in
[Readiness semantics (L3/L4)](READINESS-SEMANTICS.md); every failure code is
catalogued in [Failure code catalog](FAILURE-CODES.md).

## What this change delivers

- **A closed component set.** The state closure is an enumerated,
  required-vs-optional component set: `model`, `optimizer`, `global_step`,
  `dataloader`, `rng` are required; `lr_scheduler`, `sampler`, `rank_local` are
  optional but must still be *declared*. Every component is always present in
  the manifest (present or absent) so a partial checkpoint cannot look simpler
  than it is. The closure classifies as `COMPLETE`, `PARTIAL`, `MISSING`, or
  `MALFORMED`.
- **End-to-end closure.** Capture -> manifest -> classify -> the agent exposes
  a closure summary (protocol v2) -> the controller gates target readiness on
  closure completeness. The single classifier (Python data plane) is the source
  of truth; the Go control plane consumes the classified summary and enforces
  gating.
- **L3 vs L4, with a hard promise boundary.** L3 proves model + optimizer + N
  training steps are loadable. L4 additionally proves that dataloader / RNG /
  sampler continuity holds. L4 requires the closure to be complete **and** a
  restore drill to anchor to the same manifest digest. Readiness never claims
  L4 without both anchors; it also explicitly does **not** claim throughput,
  convergence, or final loss (see READINESS-SEMANTICS.md).
- **Two anchors.** (1) manifest `<->` record identity (uid / version /
  metadata digest); (2) drill `<->` manifest digest. A mismatch retracts
  `loadable` and blocks the target (`RESUME_COMPONENT_DIGEST_MISMATCH`), and an
  unanchored drill cannot earn L4.
- **Proof bound to the manifest.** Proof schema v2 carries
  `resume_manifest_digest`; a proof that is not bound to the manifest is
  rejected where a manifest is required.
- **A failure-code catalog enforced in both languages.** 27 reason codes are
  documented and tested against the Python and Go emission sites; the catalog
  cannot drift from the code (a test fails on either side if they disagree).

## Breaking contract changes (upgrade required)

Three wire contracts changed version. Consumers that do not upgrade are
**rejected** rather than silently degraded — fail-closed by design.

1. **Agent `/v1/checkpoint-record` is now protocol `version: "2"`** and carries
   `resumeState { closure, manifestDigest, components, reasonCodes }`. A missing
   manifest is reported as `null` (no evidence); an unparseable manifest as
   `MALFORMED` with no digest. Protocol `version: "1"` is rejected by the
   controller and surfaces as `AGENT_UNREACHABLE` with a reason. Agents must
   expose the resume-state manifest at the configured path and speak v2.
2. **`target-ready` report must be schema `v2`**, binding
   `resumeState { manifestDigest, closure, components }`. `v1` is decoded but
   not trusted; the closure evidence must come from `v2`.
3. **Proof must be schema `v2`** with `resume_manifest_digest` (nullable). A
   proof bound to a *different* manifest, or to none where a manifest is
   required, is rejected.

## Invariants this change guarantees

- **No silent Python fallback** for the native hot path (Rust / cgo kernel). A
  missing extension is a hard `KernelUnavailable`, never a quiet downgrade to
  interpreted code.
- **Fail-closed by default.** A missing or incomplete closure retracts
  `loadable` and blocks the target; an unanchored drill cannot earn L4; multiple
  agents that disagree on closure evidence lose the evidence and emit
  `RESUME_MISMATCH`.
- **The two implementations cannot drift.** The readiness conformance fixtures
  carry real manifests and are regenerated from the production summarizer; the
  Python verdict is derived from the typed manifest, and the Go verdict is
  asserted against the same serialized expected output.

## How to verify

```bash
# Python
python -m pytest tests/unit tests/integration -q
python scripts/check_repo.py

# Go
go test -count=1 ./... && go vet ./... && gofmt -l cmd internal api

# Charts
helm lint deploy/helm/aevrel

# End-to-end acceptance
python scripts/first_run_acceptance.py
python scripts/product_acceptance.py
```

## Known limitations (deferred)

- `lr_scheduler` is declared in the closure but its capture path is still under
  a separate decision; it is treated as optional and must be declared either
  way.
- Validation has been exercised against DCP-based checkpoints and the in-repo
  conformance fixtures. First-class TorchTitan / Ray adapters are future work.
