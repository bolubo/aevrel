# shardferry Helm chart

这个 chart 部署 controller、artifact PVC，以及 source/target node agents。

镜像构建（国内网络可用镜像站参数）：

```bash
docker build -f deploy/docker/Dockerfile.controller \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/shardferry/shardferry-controller:0.1.0 .

docker build -f deploy/docker/Dockerfile.agent \
  --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t ghcr.io/shardferry/shardferry-agent:0.1.0 .

docker build -f deploy/docker/Dockerfile.gateway \
  --build-arg BUILDER_IMAGE=docker.m.daocloud.io/library/golang:1.24-bookworm \
  --build-arg RUNTIME_IMAGE=gcr.m.daocloud.io/distroless/static-debian12:nonroot \
  -t ghcr.io/shardferry/shardferry-gateway:0.1.0 .
```

当前源码首发不自动发布 GHCR 镜像；上面的 tag 需要你自己构建并推送，或把
`image.controller` / `image.agent` 改成你的镜像仓库。不要把未推送的 tag
当成可拉取的生产镜像。

```bash
kubectl -n default create secret generic shardferry-agent-token \
  --from-literal=token='replace-me'

helm upgrade --install shardferry deploy/helm/shardferry \
  --set source.checkpointHostPath=/data/shardferry/job42 \
  --set source.metadataHostPath=/data/shardferry/job42-metadata \
  --set target.enabled=true \
  --set target.checkpointHostPath=/data/shardferry/target
```

source agent 使用 9443，target agent 使用 9444，可以同节点共存。source 的
`metadataHostPath` 必须在所有 source 节点可见；target 数据写入 `/targets`。

用户入口：

```bash
shardferryctl migrate -f examples/k8s/migration.yaml --dry-run
shardferryctl preflight -f examples/k8s/migration.yaml
shardferryctl migrate -f examples/k8s/migration.yaml --wait
```

`agentSelector` 由 controller 解析为 Pod 和本地 rank，不需要手写 rankMap。

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

完整示例见 [`examples/cross-cluster/README.zh-CN.md`](../../../examples/cross-cluster/README.zh-CN.md)。
