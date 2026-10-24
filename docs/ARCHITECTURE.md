# Architecture

Aevrel is a scheduler-neutral control plane for training state. It observes
checkpoint and runtime state, publishes readiness facts, and executes recovery
or mobility actions only when the evidence is sufficient.

## Boundaries

- Aevrel does not train models.
- Aevrel does not replace Kubernetes, Kueue, Volcano, Kubeflow, or a storage
  system.
- Training frameworks remain responsible for saving and loading their own
  state.
- Aevrel does not claim that an unsaved runtime state can be recovered.
- Aevrel does not treat file existence as proof that a checkpoint can be
  loaded.

## Layers

```text
training runtime / storage
        |
        v
checkpoint providers and runtime adapters
        |
        v
state facts and verification evidence
        |
        v
control-plane decision and proof
        |
        +--> CLI / UI
        +--> Kueue / Volcano / Kubeflow
        +--> restore, restart, migrate, or hold
```

### State and data plane

Python providers inspect DCP metadata, rank-local shards, state bundles, and
target-load results. These operations are read-only against the source
checkpoint.

### Control plane

Go controllers maintain the authoritative Kubernetes objects:

- `CheckpointRecord`: checkpoint identity, rank inventory, metadata digest, and
  observed state;
- `CheckpointReadiness`: complete, fresh, loadable, movable, reasons, and RTO;
- `RestoreDrill`: target validation and per-rank evidence;
- `RescueDecision`: advisory `restore`, `restart`, `migrate`, or `hold`;
- `CheckpointMigration`: static or live execution state.

### Execution

The local CLI and `aevrelctl` provide user-facing entry points. An optional
agent and gateway expose read-only inventory and target validation in a
cluster. Kueue consumes readiness facts through an adapter.

## Invariants

- Source checkpoints are never modified.
- Unknown, stale, partial, or unreadable evidence is not treated as ready.
- A target is not considered ready until a real load and required steps
  succeed.
- Load and migration proof is bound to the checkpoint version and metadata
  digest.
- A failure cannot produce a partial target that looks successful.
- Scheduler facts are separate from scheduler policy.

## Evidence flow

```text
checkpoint inventory
  -> complete / fresh
  -> restore drill or target load
  -> loadable
  -> target compatibility
  -> movable
  -> rescue decision or migration action
  -> proof / status
```

The first supported checkpoint format is DCP. Other formats and runtimes are
adapters, not alternate sources of truth.
