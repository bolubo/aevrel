# shardferry Helm chart

这个 chart 可以部署 controller、artifact PVC、source/target node agents，
以及跨集群用的 gateway。

镜像构建（国内网络可用镜像站参数）：

```bash
docker build -f deploy/docker/Dockerfile.controller \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/shardferry-controller:0.1.1 .

docker build -f deploy/docker/Dockerfile.agent \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/bolubo/shardferry-agent:0.1.1 .

docker build -f deploy/docker/Dockerfile.gateway \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/bolubo/shardferry-gateway:0.1.1 .
```

发布 tag 会通过 `.github/workflows/release.yml` 自动构建并推送
controller/gateway/agent 镜像到 `ghcr.io/bolubo/shardferry-*`。发布前必须
确认 GHCR 包为 public；否则 Helm 默认 values 仍会拉取失败。

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

## Live Rank Split（实验）

如果 LRS 执行器已经实现 `POST /v1/live/reconcile`，可以把 controller 接到
它：

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  --set controller.liveURL=http://lrs-handoff.live.svc:9460 \
  --set controller.liveToken.existingSecret=shardferry-live-token
```

不设置 `controller.liveURL` 时，static 迁移完全不受影响；但使用
`spec.mode: live-rank-split` 的 CR 会以 `LiveExecutorUnavailable` 失败关闭。
未配置 `controller.liveToken.existingSecret` 时，controller 复用
`SHARDFERRY_AGENT_TOKEN`。

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
