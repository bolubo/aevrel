# Cross-cluster example

This directory shows the minimal path for registering two clusters in a
management cluster and running a migration.

## Roles

- management cluster: controller, artifact store, and `AevrelCluster`;
- source cluster: source agents and gateway;
- target cluster: target agents and gateway.

The gateway provides local agent discovery and a reverse proxy. It is not an
overlay, VPN, or service mesh.

## Install

Management cluster:

```bash
helm upgrade --install aevrel-control deploy/helm/aevrel \
  -n aevrel-control --create-namespace \
  --set source.enabled=false \
  --set target.enabled=false \
  --set tls.enabled=true \
  --set tls.controllerSecretName=aevrel-tls \
  --set tls.agentSecretName=aevrel-tls \
  --set controller.serviceType=LoadBalancer \
  --set controller.artifactPublicURL=https://CONTROL_ARTIFACT_HOST:8082
```

Source cluster:

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  -n aevrel --create-namespace \
  --set controller.enabled=false \
  --set gateway.enabled=true \
  --set gateway.publicURL=https://SOURCE_GATEWAY_HOST:9445 \
  --set gateway.serviceType=LoadBalancer \
  --set tls.enabled=true \
  --set tls.controllerSecretName=aevrel-tls \
  --set tls.agentSecretName=aevrel-tls \
  --set source.enabled=true \
  --set target.enabled=false \
  --set controller.artifactPublicURL=https://CONTROL_ARTIFACT_HOST:8082
```

For the target cluster, swap `source.enabled` and `target.enabled`, then set
the target checkpoint hostPath.

If the chart-managed source agent is used, make it return shard URLs reachable
through the gateway:

```bash
  --set source.publicURL='https://SOURCE_GATEWAY_HOST:9445/v1/agents/{pod}'
```

The agent expands `{pod}` at runtime. This lets target agents read source
shards through the gateway instead of connecting directly to source pod IPs.

Production deployments use mTLS: the controller, gateways, and source/target
agents share one CA; gateways verify client certificates; and the agent data
plane also uses mTLS. Provide a Secret containing `ca.crt`, `tls.crt`, and
`tls.key`. Certificate SANs must cover the gateway endpoint, agent node IPs,
and artifact endpoint.

## Register and migrate

Run these commands in the management cluster:

```bash
export AEVREL_AGENT_TOKEN='replace-with-the-agent-token'
export AEVREL_TLS_CA=/path/to/ca.crt
export AEVREL_TLS_CERT=/path/to/tls.crt
export AEVREL_TLS_KEY=/path/to/tls.key
kubectl --context management apply -f clusters.yaml
kubectl --context management -n aevrel-control get aevrelclusters -o wide
aevrelctl get clusters -n aevrel-control
aevrelctl describe cluster source -n aevrel-control
aevrelctl plan -f migration.yaml
aevrelctl preflight -f migration.yaml --json
aevrelctl migrate -f migration.yaml --wait
aevrelctl describe migration job42-16-to-8 -n aevrel-control --ranks
```

Lifecycle commands:

```bash
aevrelctl cancel migration job42-16-to-8 -n aevrel-control
aevrelctl retry migration job42-16-to-8 -n aevrel-control
aevrelctl delete migration job42-16-to-8 -n aevrel-control
aevrelctl delete migration job42-16-to-8 -n aevrel-control --purge-target
```

`cancel` is allowed only before the commit intent. `retry` currently repairs
only `commit-incomplete` state and does not transfer already committed ranks
again. `delete` preserves the target by default and removes only controller
artifacts and staging. `--purge-target` explicitly removes the target
directory; the source is never deleted.

`preflight` reads source inventory through the gateway and probes target agent
capabilities. It does not merely validate YAML. The JSON `clusters` field
reports each gateway's `ready`, `version`, and `lastSeen`.

### Credential model

- the token in `AevrelCluster.secretRef` authenticates that cluster's gateway;
- the controller binds a token to each gateway endpoint instead of using one
  global gateway token;
- `AEVREL_AGENT_TOKEN` is used only by local agents and the source-shard data
  path;
- `/v1/shards/*` on the gateway accepts the agent token, while other proxy
  paths require the local gateway token.

The token remains an application-layer second factor; mTLS establishes
transport identity.

### Live validation (2026-10-22)

The cluster health/status controller and mTLS gateway were exercised with:

- `AevrelCluster.status.ready=true` for both `source` and `target`;
- preflight: 16/16 source ranks and 8/8 target ranks;
- the 16-to-8 live migration reached `phase=succeeded` with 8/8 ranks
  committed;
- all eight copied rank-local DCP checkpoints passed `aevrel-target-load`;
- per-cluster gateway tokens and mTLS, with no dependency on remote pod IPs.

### Register a cluster

`cluster add` only generates a local manifest. It does not connect to a
cluster, scan it, or modify it:

```bash
aevrelctl cluster add source \
  --endpoint https://source-gateway.example:9445 \
  --role source \
  --secret source-auth \
  --file clusters-source.yaml
```

The output is a multi-document YAML file: the `Secret` contains only `token`,
and `AevrelCluster` points at that Secret. `role` defaults to `both`; when
`--secret` is omitted, `<cluster-name>-gateway-auth` is used, and when
`--token` is omitted, a random 32-byte token is generated. The file is created
with mode `0600` and is never overwritten. `--json` emits an equivalent
Kubernetes `List`. Review the output and apply it explicitly:

```bash
kubectl apply -f clusters-source.yaml -n aevrel-control
```

## Validation record (2026-10-22, two kind clusters)

- 16 source ranks to 8 target ranks: `succeeded`, 8/8 target ranks committed,
  all eight target DCP checkpoints loaded locally, and the source checkpoint
  SHA256 remained unchanged.
- 128 source ranks to 64 target ranks: `succeeded`, 64/64 target ranks
  committed, all 64 target DCP checkpoints loaded locally, and the report
  contained 64 rank reports and 64 proofs.
- 1 GiB, 128 to 64 data path: `succeeded`, all 64 target DCP checkpoints
  loaded locally, source SHA256 unchanged, `full_gather=false`, and the report
  contained 64 reports and 64 proofs. This is a control/data-path closure, not
  a performance benchmark.
- Stopping the source gateway made `preflight` fail explicitly; an existing
  successful migration was unaffected.
- Stopping a target agent made `preflight` fail during resolve without
  creating a migration.
- A target root outside the agent's allowed roots failed preflight. Inside an
  allowed root, preflight checks parent writability, free space, and whether
  the path is non-empty.
- After a controller restart, an already successful migration remained
  `succeeded` and did not silently rerun.
- Restarting the controller during a 1 GiB migration resumed from the execution
  snapshot and reached `commit-pending`, then `succeeded`, with 64/64 loads and
  complete reports/proofs.
- Per-cluster tokens: source and target used distinct gateway tokens, and both
  the 16-to-8 flow and the 1 GiB controller-restart flow passed without a
  global gateway token.
- Cluster health: `get clusters` and `describe cluster` reported gateway
  readiness, endpoint, version, and errors.
- Rank observability: `describe migration --ranks` showed phase, task, retries,
  and error for every target rank.
- Plan preview: `aevrelctl plan -f ...` reported bytes, theoretical minimum,
  full gather, nodes, range/fan-in, and target roots without creating
  resources.
- mTLS: the controller, gateway, and agent used one CA; the 16-to-8 flow
  succeeded with 8/8 target loads. A protected resolve request without a
  client certificate returned 401, while health remained 200.

The 128-to-64 run used small tensors and small DCP files. It validates the
control plane and rank orchestration, not 1 GiB/10 GiB performance or real GPU
behavior.

Delete the test clusters with:

```bash
kind delete cluster --name aevrel-e2e-source
kind delete cluster --name aevrel-e2e-target
```
