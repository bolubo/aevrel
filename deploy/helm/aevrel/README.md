# Aevrel Helm chart

This chart deploys the controller, artifact PVC, source and target node agents,
the optional LRS live worker, and the gateway used for cross-cluster access.

## Build images

The build arguments can point at regional mirrors when direct access to the
default registries is slow:

```bash
docker build -f deploy/docker/Dockerfile.controller \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.26-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/aevrel-controller:0.4.0 .

docker build -f deploy/docker/Dockerfile.agent \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/bolubo/aevrel-agent:0.4.0 .

docker build -f deploy/docker/Dockerfile.gateway \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.26-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/aevrel-gateway:0.4.0 .

docker build -f deploy/docker/Dockerfile.live-worker \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/bolubo/aevrel-live-worker:0.4.0 .

docker build -f deploy/docker/Dockerfile.kueue-adapter \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.26-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/aevrel-kueue-adapter:0.4.0 .
```

A release tag triggers `.github/workflows/release.yml`, which builds and
pushes the controller, gateway, agent, live-worker, and kueue-adapter images
to `ghcr.io/bolubo/aevrel-*`. Confirm that the GHCR packages are public before
announcing a release; otherwise the default Helm values cannot pull them.

## Install

```bash
kubectl -n default create secret generic aevrel-agent-token \
  --from-literal=token='replace-me'

helm upgrade --install aevrel deploy/helm/aevrel \
  --set source.checkpointHostPath=/data/aevrel/job42 \
  --set source.metadataHostPath=/data/aevrel/job42-metadata \
  --set target.enabled=true \
  --set target.checkpointHostPath=/data/aevrel/target
```

The source agent listens on 9443 and the target agent on 9444, so both can run
on the same node. Only the source node selected by `metadataOwnerRank` needs
access to `.metadata`; the controller distributes this small file to peer
agents without a shared directory. If every source node already mounts the
same metadata path, `metadataHostPath` can be used instead. Target data is
written under `/targets`.

User-facing commands:

```bash
aevrelctl migrate -f examples/k8s/migration.yaml --dry-run
aevrelctl preflight -f examples/k8s/migration.yaml
aevrelctl migrate -f examples/k8s/migration.yaml --wait
```

The controller resolves `agentSelector` to pods and local ranks, so a
hand-written `rankMap` is not required.

## Live Rank Split (CPU contract worker)

The chart includes an optional Python LRS worker that implements
`POST /v1/live/reconcile`. When enabled, the controller automatically points
at the worker Service from the same release:

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set liveWorker.enabled=true \
  --set liveWorker.token.existingSecret=aevrel-live-token
```

The built-in worker can be disabled and `controller.liveURL` can point at an
external executor instead. With neither configured, static migration is
unaffected; a CR using `spec.mode: live-rank-split` fails closed with
`LiveExecutorUnavailable`.

The current worker is a **CPU contract backend**. It owns idempotent phases,
coverage, quorum, proof, and durable recovery, and does not claim continuous
GPU collectives. Four-GPU, two-node validation remains a later gate.

## Kueue preemption guard (optional)

The Kueue adapter is an optional chart component. Install Kueue with
`ConfigurablePreemptions` enabled, then enable the adapter:

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

The chart creates the adapter, RBAC, and a `PreemptionConfig` named
`checkpoint-guard` by default. The target ClusterQueue must reference it:

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

With `strict=true`, the adapter validates the strict profile before startup.
Missing configuration makes the process exit instead of running in a state
that only appears protected. Production defaults use `fallbackValue: 1`; an
advisory rollout may set it to `0`, but that is not binary protection.

The adapter reads `CheckpointRecord` first (`observation-source=auto`) and
falls back to `aevrel.dev/*` annotations only when the Workload or Job has no
matching record. Inspect the result with:

```bash
aevrelctl kueue status
aevrelctl kueue explain <workload>
```

## Cross-cluster access

Do not rely on remote pod IPs across clusters. Enable a thin gateway in each
source and target cluster, then let the controller in the management cluster
reach it through `AevrelCluster`:

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  -n aevrel --create-namespace \
  --set controller.enabled=false \
  --set gateway.enabled=true \
  --set gateway.publicURL=http://gateway.example:9445 \
  --set gateway.serviceType=LoadBalancer \
  --set tls.enabled=false \
  --set source.enabled=true \
  --set target.enabled=false \
  --set controller.artifactPublicURL=http://control-artifacts.example:8082
```

The gateway only discovers local agents and provides a reverse proxy. Source
and target agents still obtain specs and plans from
`controller.artifactPublicURL`; the artifact Service in the management cluster
must be reachable from both workload clusters.

## mTLS

Cross-cluster transport uses mTLS by default. Create a Secret containing the
CA and client material:

```bash
kubectl -n aevrel create secret generic aevrel-tls \
  --from-file=ca.crt=ca.crt \
  --from-file=tls.crt=tls.crt \
  --from-file=tls.key=tls.key
```

The certificate SANs must cover the gateway `publicURL`, agent node addresses,
and the controller artifact endpoint. Enable TLS during installation:

```bash
--set tls.enabled=true \
--set tls.controllerSecretName=aevrel-tls \
--set tls.agentSecretName=aevrel-tls
```

Gateway and controller HTTPS probes, source-shard TLS from the agent, and
artifact TLS all use the same material. CLI preflight reads
`AEVREL_TLS_CA`, `AEVREL_TLS_CERT`, and `AEVREL_TLS_KEY`.

See the [cross-cluster example](../../../examples/cross-cluster/README.md) for
the complete setup.
