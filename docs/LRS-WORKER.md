# LRS Worker

`aevrel-live-worker` implements the HTTP backend used by the Go Live Rank Split
controller.

## Role

```text
Go controller
  -> POST /v1/live/reconcile
  -> LRS worker
  -> phase / coverage / quorum / stateComplete / proofRef
  -> controller target-ready gate
```

The controller remains the authority for `CheckpointMigration` status. The
worker returns observations and does not modify the custom resource.

## Start

```bash
aevrel-live-worker \
  --listen 0.0.0.0:9460 \
  --state-dir /var/lib/aevrel/live-worker \
  --token "$AEVREL_LIVE_TOKEN"
```

Local development may omit the token. A deployed setup should use the same
Secret for the worker and controller.

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set liveWorker.enabled=true \
  --set liveWorker.token.existingSecret=aevrel-live-token
```

## Protocol

The worker accepts `LiveRankSplitReconcile` requests and returns a response
such as:

```json
{
  "phase": "following",
  "inFlight": [0, 1],
  "coverage": [0, 1, 2, 3],
  "quorum": 3,
  "stateComplete": false,
  "stateRoot": "/state/step-100/rank{rank}",
  "proofRef": "",
  "message": "2 rank(s) in flight",
  "ranks": [
    {"rank": 0, "phase": "in_flight", "owner": 0, "epoch": 1}
  ]
}
```

On success, `inFlight` is empty, coverage includes every logical rank, and a
readable proof reference is returned. If target validation is configured, the
controller waits for the target Job before completing the migration.

## Persistence

The worker stores tick and proof state under `--state-dir`. A controller or
worker restart resumes from the persisted tick rather than restarting the
migration from preflight.

## Limitations

- The current backend is a CPU contract backend.
- It validates control-plane state, phase, coverage, quorum, proof, and restart
  recovery.
- It does not claim CUDA/NCCL collective continuity.
- A real training backend must preserve the same HTTP contract and remain
  behind the controller's target-ready gate.
