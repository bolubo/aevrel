# Kubernetes local-disk deployment scaffold

This is not an operator. It is a Job and StatefulSet scaffold for a 128-node
local-disk environment. It starts a per-rank source agent and a per-rank target
writer; it does not discover training jobs or drive a scheduler.

## Source

`source-service.yaml` provides a headless Service.
`source-statefulset.yaml` gives every source rank one pod and one local PVC.
Each pod runs:

```text
serve --rank <ordinal> --listen 0.0.0.0:9000
```

`migration.yaml` is stored in the `aevrel-source-spec` ConfigMap. The
`/checkpoints` directory in each pod contains that rank's local shard.

## Rank map

Render `rank_map` before generating the client spec:

```bash
python scripts/render_rank_map.py \
  --service aevrel-source --namespace default \
  --world-size 128 --port 9000 --out rank-map.yaml
```

Merge the rank map into `source.mesh.rank_map` in `migration.yaml`, then let
the target side fetch it with `fetch-spec`.

## Target

`target-statefulset.yaml` requires two ConfigMaps:

- `aevrel-target-spec`: contains `client.yaml` and `plan.json`;
- a local PVC for each target pod, mounted at `/checkpoints`.

Each pod runs:

```text
apply --spec /config/client.yaml --plan /config/plan.json \
  --target-rank <ordinal> --target-root /checkpoints
```

After all ranks finish, the coordinator collects every rank report and runs:

```bash
aevrel summarize \
  --reports-dir ./reports \
  --spec ./client.yaml \
  --plan ./plan.json \
  --out migration-summary.json
```

With `--spec` and `--plan`, `summarize` fails closed: a missing target rank,
duplicate rank, non-success state, validation failure, or chunk/planned-byte
mismatch exits with status 2. Omit both arguments only for an ad hoc summary.

## Before production use

- choose a real local PV, hostPath, or PVC layout;
- build and pin images by digest;
- collect reports from every target node into one directory (this repository
  only provides summarization and completeness validation);
- define retry and Job rerun policy;
- replace the one-shot StatefulSet target with a target agent or a
  completion-aware Job/operator, because kubelet restarts an exited container;
- add TLS/tokens or restrict traffic to a trusted network;
- validate a real multi-node A/B deployment and 128-rank GPU workload.
