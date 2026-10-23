# 跨集群示例

这个目录演示“用户在管理集群写两个集群，然后执行迁移”的最小路径。

## 角色

- 管理集群：controller、artifact store、`AevrelCluster`；
- source 集群：source agents + gateway；
- target 集群：target agents + gateway。

gateway 只做本集群 agent 发现和反向代理，不是 overlay/VPN/service mesh。

## 安装

管理集群：

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

source 集群：

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

target 集群把 `source.enabled` 和 `target.enabled` 对调，并设置 target 的
checkpoint hostPath。

如果使用 chart 自带的 source agent，还要让它返回 gateway 可达的 shard URL：

```bash
  --set source.publicURL='https://SOURCE_GATEWAY_HOST:9445/v1/agents/{pod}'
```

agent 会在运行时展开 `{pod}`；这是为了让 target agent 通过 gateway
读取 source shard，而不是直接访问 source Pod IP。

生产方向使用 mTLS：controller、gateway、source/target agent 使用同一 CA，
gateway 校验客户端证书，agent 数据面也走 mTLS。示例需要提供包含
`ca.crt`、`tls.crt`、`tls.key` 的 Secret，证书 SAN 覆盖 gateway endpoint、
agent 节点 IP 和 artifact endpoint。

## 注册和迁移

在管理集群：

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

生命周期命令：

```bash
aevrelctl cancel migration job42-16-to-8 -n aevrel-control
aevrelctl retry migration job42-16-to-8 -n aevrel-control
aevrelctl delete migration job42-16-to-8 -n aevrel-control
aevrelctl delete migration job42-16-to-8 -n aevrel-control --purge-target
```

`cancel` 只允许在 commit intent 前；`retry` 当前只修复
`commit-incomplete`，不会把已提交的 rank 重新传一遍。`delete` 默认保留
target，只清理 controller artifact 和 staging；`--purge-target` 才显式删除
target 目录，source 永不被删除。

`preflight` 会通过 gateway 实际读取 source inventory，并探测 target
agent capabilities；不是只检查 YAML。JSON 输出里的 `clusters` 会显示
gateway 的 `ready/version/lastSeen`。

### 凭据模型

- `AevrelCluster.secretRef` 的 token 是该集群 gateway token；
- controller 按 gateway endpoint 绑定各自 token，不再依赖全局 gateway token；
- `AEVREL_AGENT_TOKEN` 只用于本地 agent 和 source shard 数据路径；
- gateway 的 `/v1/shards/*` 允许 agent token，其他 proxy 路径要求本集群
  gateway token。

当前 token 仍保留为应用层第二因子；mTLS 负责传输身份。

### P0 live acceptance（2026-10-22）

使用新的 cluster health/status controller 和 mTLS gateway 实际跑过：

- `source` 和 `target` 的 `AevrelCluster.status.ready=true`；
- preflight：16/16 source ranks、8/8 target ranks；
- `p0-live-16-to-8`：`phase=succeeded`，8/8 committed；
- target 复制的 8 个 rank-local DCP 全部通过 `aevrel-target-load`；
- 使用 per-cluster gateway token + mTLS，不依赖远程 Pod IP。

### 注册集群

`cluster add` 只生成本地 manifest，不连接、不扫描、也不会自动修改任何集群：

```bash
aevrelctl cluster add source \
  --endpoint https://source-gateway.example:9445 \
  --role source \
  --secret source-auth \
  --file clusters-source.yaml
```

输出是一份多文档 YAML：`Secret` 只含 `token`，`AevrelCluster` 指向该
Secret。`role` 默认 `both`；省略 `--secret` 时使用
`<cluster-name>-gateway-auth`；省略 `--token` 时生成 32 字节随机 token。
文件以 `0600` 创建且拒绝覆盖。`--json` 输出等价的 Kubernetes `List`。
确认内容后再显式应用：

```bash
kubectl apply -f clusters-source.yaml -n aevrel-control
```

## 已验证（2026-10-22，两套 kind）

- 16 source ranks → 8 target ranks：`succeeded`，8/8 target rank committed，
  target DCP 8/8 本地 load 通过，source checkpoint SHA256 不变。
- 128 source ranks → 64 target ranks：`succeeded`，64/64 target rank committed，
  target DCP 64/64 本地 load 通过，report 包含 64 个 rank reports 和 64 个 proof。
- 1 GiB / 128→64 数据面：`succeeded`，target DCP 64/64 本地 load 通过，
  source SHA256 不变，plan `full_gather=false`，report 包含 64 reports + 64 proofs。
  这是控制面/数据面闭环，不是性能基准。
- source gateway 停止时，`preflight` 明确失败，已有成功 migration 不受影响。
- target agent 停止时，`preflight` 在 resolve 阶段明确失败，不创建迁移。
- target root 在 agent 允许根之外时，`preflight` 明确失败；允许根内会检查
  父目录可写、剩余空间和路径非空状态。
- controller 重启后，已成功 migration 仍保持 `succeeded`，不会静默重跑。
- 1GiB 迁移进行中重启 controller：从 execution snapshot 续跑，
  最终 `commit-pending → succeeded`，64/64 load，report/proof 完整。
- per-cluster token：source/target 使用各自 gateway token，16→8 和 1GiB
  controller 重启路径均通过；不再是单一全局 gateway token。
- cluster health：`get clusters` / `describe cluster` 已实测显示 gateway
  ready、endpoint、version 和错误信息。
- rank observability：`describe migration --ranks` 显示每个 target rank 的
  phase、task、retries 和 error。
- plan preview：`aevrelctl plan -f ...` 已实测输出 bytes、
  theoretical min、full gather、nodes、range/fan-in 和 target roots，不创建资源。
- mTLS：controller/gateway/agent 使用同一 CA；16→8 全链路成功，target
  8/8 load；无客户端证书访问受保护 resolve 返回 401，health 保持 200。

这组 128→64 验证使用小 tensor/小 DCP 数据，只证明控制面和 rank 编排，
不是 1GiB/10GiB 性能或真实 GPU 结论。

清理测试集群：

```bash
kind delete cluster --name aevrel-e2e-source
kind delete cluster --name aevrel-e2e-target
```
