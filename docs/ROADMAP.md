# Roadmap

Aevrel is a scheduler-neutral training-state control plane. The current focus
is making checkpoint state observable, verifiable, and usable by recovery and
scheduler workflows.

## Current focus

### Resume-state closure

Define the state required to continue training:

- model and optimizer state;
- RNG and sampler state;
- dataloader position;
- scheduler, epoch, and global step;
- topology and rank-local state.

The first supported slice is DCP-based checkpoint inspection and restore
validation. Additional runtime adapters will be added only when they can be
tested against a real runtime.

### Scheduler and runtime contract

Publish checkpoint facts in a scheduler-neutral form:

- complete, fresh, loadable, and movable;
- restore mode;
- observation time and evidence source;
- stable reason codes;

Kueue is the first adapter. Aevrel does not replace Kueue, Volcano, Kubeflow, or
the scheduler's own admission policy.

### Readiness and verification

Make readiness evidence trustworthy:

- distinguish missing, incomplete, stale, and partial evidence;
- require a real target load and training steps for `loadable`;
- bind proof to checkpoint version and metadata digest;
- invalidate proof when the checkpoint changes;
- keep source checkpoints read-only.

## Near-term milestones

1. Complete one real runtime integration and one real checkpoint fixture.
2. Run a pilot on a non-critical training workflow.
3. Add failure-path and scale coverage for the supported envelope.
4. Add scheduler consumption tests against Kueue facts.

## Not promised

- No release-date commitments.
- No claim that every training framework is supported.
- No claim that unsaved state can be recovered.
- No replacement for a training framework or a scheduler.
