# 跨集群示例

这个目录演示“用户在管理集群写两个集群，然后执行迁移”的最小路径。

## 角色

- 管理集群：controller、artifact store、`ShardferryCluster`；
- source 集群：source agents + gateway；
- target 集群：target agents + gateway。

gateway 只做本集群 agent 发现和反向代理，不是 overlay/VPN/service mesh。

## 安装

管理集群：

```bash
helm upgrade --install shardferry-control deploy/helm/shardferry \
  -n shardferry-control --create-namespace \
  --set source.enabled=false \
  --set target.enabled=false \
  --set controller.serviceType=LoadBalancer \
  --set controller.artifactPublicURL=http://CONTROL_ARTIFACT_HOST:8082
```

source 集群：

```bash
helm upgrade --install shardferry deploy/helm/shardferry \
  -n shardferry --create-namespace \
  --set controller.enabled=false \
  --set gateway.enabled=true \
  --set gateway.publicURL=http://SOURCE_GATEWAY_HOST:9445 \
  --set gateway.serviceType=LoadBalancer \
  --set tls.enabled=false \
  --set source.enabled=true \
  --set target.enabled=false \
  --set controller.artifactPublicURL=http://CONTROL_ARTIFACT_HOST:8082
```

target 集群把 `source.enabled` 和 `target.enabled` 对调，并设置 target 的
checkpoint hostPath。

如果使用 chart 自带的 source agent，还要让它返回 gateway 可达的 shard URL：

```bash
  --set source.publicURL='http://SOURCE_GATEWAY_HOST:9445/v1/agents/{pod}'
```

agent 会在运行时展开 `{pod}`；这是为了让 target agent 通过 gateway
读取 source shard，而不是直接访问 source Pod IP。

P0 alpha 先走 token + 可信网络，source/target agent 与 gateway 都关闭 TLS；
mTLS 是下一轮硬化项。

## 注册和迁移

在管理集群：

```bash
export SHARDFERRY_AGENT_TOKEN='replace-with-the-agent-token'
kubectl --context management apply -f clusters.yaml
shardferryctl preflight -f migration.yaml --json
shardferryctl migrate -f migration.yaml --wait
```

`preflight` 会通过 gateway 实际读取 source inventory，并探测 target
agent capabilities；不是只检查 YAML。

### P0 凭据简化

当前示例使用同一份 agent token 贯穿 gateway 和 agent，避免第一版引入
per-cluster token 路由。`ShardferryCluster.secretRef` 的 token 必须与
对应 gateway 的 `SHARDFERRY_GATEWAY_TOKEN` 一致；controller 的
`SHARDFERRY_AGENT_TOKEN` 必须与 agent/gateway 的代理 token 一致。

后续会把 gateway token、agent token 和每集群凭据拆开。

## 已验证（2026-10-22，两套 kind）

- 16 source ranks → 8 target ranks：`succeeded`，8/8 target rank committed，
  target DCP 8/8 本地 load 通过，source checkpoint SHA256 不变。
- 128 source ranks → 64 target ranks：`succeeded`，64/64 target rank committed，
  target DCP 64/64 本地 load 通过，report 包含 64 个 rank reports 和 64 个 proof。
- 1 GiB / 128→64 数据面：`succeeded`，target DCP 64/64 本地 load 通过，
  source SHA256 不变，plan `full_gather=false`，report 包含 64 reports + 64 proofs。
  这是控制面/数据面闭环，不是性能基准。
- source gateway 停止时，`preflight` 明确失败，已有成功 migration 不受影响。
- target root 在 agent 允许根之外时，`preflight` 明确失败；允许根内会检查
  父目录可写、剩余空间和路径非空状态。
- controller 重启后，已成功 migration 仍保持 `succeeded`，不会静默重跑。

这组 128→64 验证使用小 tensor/小 DCP 数据，只证明控制面和 rank 编排，
不是 1GiB/10GiB 性能或真实 GPU 结论。

清理测试集群：

```bash
kind delete cluster --name shardferry-e2e-source
kind delete cluster --name shardferry-e2e-target
```
