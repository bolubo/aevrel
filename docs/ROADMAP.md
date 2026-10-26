# Roadmap

Aevrel is a scheduler-neutral training-state control plane. The current focus
is making checkpoint state observable, verifiable, and usable by recovery and
scheduler workflows.

## Current focus

All three focus areas below are implemented and shipped in
[0.4.0](RELEASE-NOTES-0.4.0.md). The near-term milestones at the bottom of
this page are the current focus.

### Resume-state closure

Define the state required to continue training:

- model and optimizer state;
- RNG and sampler state;
- dataloader position;
- scheduler, epoch, and global step;
- topology and rank-local state.

The DCP-based inspection and closure-gating slice is implemented: capture to a
manifest, classify into COMPLETE/PARTIAL/MISSING/MALFORMED, expose the closure
summary over the agent protocol (v2), and gate target readiness (L3/L4) on
closure completeness. See [Resume-state closure contract](RESUME-STATE.md) and
[implementation and upgrade notes](RESUME-CLOSURE-CHANGES.md). Additional
runtime adapters will be added only when they can be tested against a real
runtime.

### Scheduler and runtime contract

Publish checkpoint facts in a scheduler-neutral form:

- complete, fresh, loadable, and movable;
- restore mode;
- observation time and evidence source;
- stable reason codes;

Kueue is the first adapter. Aevrel does not replace Kueue, Volcano, Kubeflow, or
the scheduler's own admission policy.

Contract v1 is published: see
[scheduler-runtime-contract-v1.md](scheduler-runtime-contract-v1.md) for the
envelope model, the field and label semantics, the conformance gate, and the
fail-closed matrix. `aevrelctl kueue explain` reports the facts, and the Kueue
adapter can publish them as opt-in labels (`--envelope-labels`). The Volcano and
Kubeflow translators are reserved interfaces and report themselves as
unsupported.

Still open: consuming these facts in a real Kueue preemption configuration, and
any consumer-side weighting, which stays consumer policy.

### Readiness and verification

Make readiness evidence trustworthy:

- distinguish missing, incomplete, stale, and partial evidence;
- require a real target load and training steps for `loadable`;
- bind proof to checkpoint version and metadata digest;
- invalidate proof when the checkpoint changes;
- keep source checkpoints read-only.

The semantics and the invalidation rules are published in
[READINESS-SEMANTICS.md](READINESS-SEMANTICS.md): every field names the evidence
it needs and what withdraws it, both implementations are held to the same
reason codes, and the shared fixtures fail on either side when the two drift.

## Near-term milestones

These are the current focus now that the three areas above have shipped:

1. Complete one real runtime integration and one real checkpoint fixture.
2. Run a pilot on a non-critical training workflow.
3. Add failure-path and scale coverage for the supported envelope.
4. Add scheduler consumption tests against Kueue facts.

## Not promised

- No release-date commitments.
- No claim that every training framework is supported.
- No claim that unsaved state can be recovered.
- No replacement for a training framework or a scheduler.
