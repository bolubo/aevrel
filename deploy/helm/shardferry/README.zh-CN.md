# shardferry Helm chart

这个 chart 可以部署 controller、artifact PVC、source/target node agents、
可选 LRS live worker，以及跨集群用的 gateway。

镜像构建（国内网络可用镜像站参数）：

```bash
docker build -f deploy/docker/Dockerfile.controller \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/shardferry-controller:0.2.0-rc.1 .

docker build -f deploy/docker/Dockerfile.agent \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/bolubo/shardferry-agent:0.2.0-rc.1 .

docker build -f deploy/docker/Dockerfile.gateway \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/shardferry-gateway:0.2.0-rc.1 .

docker build -f deploy/docker/Dockerfile.live-worker \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/bolubo/shardferry-live-worker:0.2.0-rc.1 .

docker build -f deploy/docker/Dockerfile.kueue-adapter \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/shardferry-kueue-adapter:0.2.0-rc.1 .
```

发布 tag 会通过 `.github/workflows/release.yml` 自动构建并推送
controller/gateway/agent/live-worker/kueue-adapter 镜像到
`ghcr.io/bolubo/shardferry-*`。发布前必须确认 GHCR 包为 public；否则 Helm
默认 values 仍会拉取失败。

```bash
kubectl -n default create secret generic shardferry-agent-token \
  --from-literal=token='replace-me'

helm upgrade --install shardferry deploy/helm/shardferry \
  --set source.checkpointHostPath=/data/shardferry/job42 \
  --set source.metadataHostPath=/data/shardferry/job42-metadata \
  --set target.enabled=true \
  --set target.checkpointHostPath=/data/shardferry/target
```

source agent 使用 9443，target agent 使用 9444，可以同节点共存。只有
`metadataOwnerRank` 对应的 source 节点需要看到 `.metadata`；controller 会
把这份小文件分发给没有共享目录的 peer agents。若所有 source 节点本来就挂载
同一个 metadata 路径，也可以继续使用 `metadataHostPath`。target 数据写入
`/targets`。

用户入口：

```bash
shardferryctl migrate -f examples/k8s/migration.yaml --dry-run
shardferryctl preflight -f examples/k8s/migration.yaml
shardferryctl migrate -f examples/k8s/migration.yaml --wait
```

`agentSelector` 由 controller 解析为 Pod 和本地 rank，不需要手写 rankMap。

## Live Rank Split（CPU contract worker）

chart 内置可选的 Python LRS worker，实现
`POST /v1/live/reconcile`。打开后 controller 会自动指向同 release 的
worker Service：

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  --set liveWorker.enabled=true \
  --set liveWorker.token.existingSecret=shardferry-live-token
```

也可以关闭内置 worker，用 `controller.liveURL` 指向外部执行器。未启用
live worker 且没有 `controller.liveURL` 时，static 不受影响；使用
`spec.mode: live-rank-split` 的 CR 会以 `LiveExecutorUnavailable` 失败关闭。

当前 worker 是 **CPU contract backend**：负责幂等 phase、coverage、quorum、
proof 和持久化恢复，不声称 GPU collective 连续；4 GPU/2 节点验证仍是后续
门禁。

## Kueue preemption guard（可选）

Kueue adapter 已作为 chart 的可选组件接入。先安装 Kueue 并启用
`ConfigurablePreemptions`，再打开 adapter：

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

chart 会创建 adapter、RBAC、`PreemptionConfig`（默认 `checkpoint-guard`）。
还需要平台把目标 ClusterQueue annotation 指向它：

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

`strict=true` 时 adapter 启动前会校验 strict profile；缺少配置会退出，
不会在“看起来 protected”的状态下运行。生产默认使用
`fallbackValue: 1`；advisory 试运行可显式设为 `0`，但那不是二元保护。

adapter 默认优先读取 `CheckpointRecord`（`observation-source=auto`），
只有当 Workload/Job 没有对应 record 时才回退到 `shardferry.io/*`
annotations。检查结果：

```bash
shardferryctl kueue status
shardferryctl kueue explain <workload>
```

## 跨集群

跨集群时不要依赖远程 Pod IP。每个 source/target 集群启用一个薄 gateway，
管理集群里的 controller 通过 `ShardferryCluster` 访问它：

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  -n shardferry --create-namespace \
  --set controller.enabled=false \
  --set gateway.enabled=true \
  --set gateway.publicURL=http://gateway.example:9445 \
  --set gateway.serviceType=LoadBalancer \
  --set tls.enabled=false \
  --set source.enabled=true \
  --set target.enabled=false \
  --set controller.artifactPublicURL=http://control-artifacts.example:8082
```

gateway 只做本集群 agent 发现和反向代理。source/target agent 仍通过
`controller.artifactPublicURL` 获取 spec/plan；管理集群的 artifact Service
需要让两个业务集群可达。

## mTLS

跨集群传输默认走 mTLS。准备一个 Secret：

```bash
kubectl -n shardferry create secret generic shardferry-tls \
  --from-file=ca.crt=ca.crt \
  --from-file=tls.crt=tls.crt \
  --from-file=tls.key=tls.key
```

证书 SAN 必须覆盖 gateway 的 `publicURL`、agent 节点地址和 controller
artifact endpoint。安装时设置：

```bash
--set tls.enabled=true \
--set tls.controllerSecretName=shardferry-tls \
--set tls.agentSecretName=shardferry-tls
```

gateway/controller 的 HTTPS probe、agent 出站 source shard TLS 和 artifact
TLS 都使用同一套材料；CLI preflight 使用
`SHARDFERRY_TLS_CA`、`SHARDFERRY_TLS_CERT`、`SHARDFERRY_TLS_KEY`。

完整示例见 [`examples/cross-cluster/README.zh-CN.md`](../../../examples/cross-cluster/README.zh-CN.md)。
