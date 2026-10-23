# Kueue preemption guard 示例

这是 `0.2 Part 1` 的最小接入包。它只同步 checkpoint 状态到 Workload label，
不修改 Kueue 源码。安装生产路径优先使用 Helm：

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

默认 `observation-source=auto`：优先关联 `CheckpointRecord`，没有 record
时才读取 Job annotations。

## 前置

- Kubernetes 集群；
- Kueue，并启用 `ConfigurablePreemptions`；
- 一个已经存在的 LocalQueue / ClusterQueue；
- 训练 Job 能写 `aevrel.dev/*` annotations。

## 1. 构建并加载镜像

```bash
docker build \
  -f deploy/docker/Dockerfile.kueue-adapter \
  -t aevrel-kueue-adapter:dev .
kind load docker-image aevrel-kueue-adapter:dev
```

## 2. 安装 RBAC 和 adapter

```bash
kubectl apply -f examples/kueue-adapter/rbac.yaml
kubectl apply -f examples/kueue-adapter/deployment.yaml
kubectl -n aevrel-system rollout status deploy/aevrel-kueue-adapter
```

示例 Deployment 使用 `--strict=true`：启动前会校验 `checkpoint-guard`
必须存在、ClusterQueue 的 classical preemption 三个入口必须为 `Never`，
并且 numeric-label 规则必须是 `maxValue=0` + `fallbackValue=1`。校验失败
会直接退出，不允许在“看起来 protected、实际上可能被抢占”的状态下运行。

## 3. 应用候选过滤规则

```bash
kubectl apply -f examples/kueue-adapter/preemption-config.yaml
```

这个文件是 strict profile：`fallbackValue: 1`，意味着没有显式
`preemption-protected=0` 的 Workload 默认被排除在候选之外。真正明确的
`eligible` 才允许被抢占。需要试运行/fail-open 时使用
`preemption-config-advisory.yaml`，但不要把它当成二元保护。

然后让目标 ClusterQueue 引用它：

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

## 4. 提交训练 Job

把 `job-publisher.yaml` 里的 queue name、image 和 command 换成真实值：

```bash
kubectl apply -f examples/kueue-adapter/job-publisher.yaml
kubectl get workloads.kueue.x-k8s.io -w
```

Workload 上应出现：

```text
aevrel.dev/preemption-protected=1
aevrel.dev/preemption-reason=ProgressThreshold
aevrel.dev/training-progress-percent=85
aevrel.dev/checkpoint-complete=1
```

查看产品化状态：

```bash
aevrelctl kueue status
aevrelctl kueue explain training
```

## 验收边界

- 本示例验证“Kueue 能看到过滤 label”，不等同于“保护绝对生效”；
- 检查 ClusterQueue 的 classical preemption 设置，避免它绕过过滤规则；
- strict profile 还要求 FairSharing 不参与抢占；
- 真实抢占验收必须观察 Kueue 事件和 victim，而不能只看 label。
