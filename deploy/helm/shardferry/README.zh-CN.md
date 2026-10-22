# shardferry Helm chart

这个 chart 部署 controller、artifact PVC，以及 source/target node agents。

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
shardferryctl migrate -f examples/k8s/migration.yaml --wait
```

`agentSelector` 由 controller 解析为 Pod 和本地 rank，不需要手写 rankMap。
