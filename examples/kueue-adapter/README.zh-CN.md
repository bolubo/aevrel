# Kueue preemption guard 示例

这是 `0.2 Part 1` 的最小接入包。它只同步 checkpoint 状态到 Workload label，
不修改 Kueue 源码。

## 前置

- Kubernetes 集群；
- Kueue，并启用 `ConfigurablePreemptions`；
- 一个已经存在的 LocalQueue / ClusterQueue；
- 训练 Job 能写 `shardferry.io/*` annotations。

## 1. 构建并加载镜像

```bash
docker build \
  -f deploy/docker/Dockerfile.kueue-adapter \
  -t shardferry-kueue-adapter:dev .
kind load docker-image shardferry-kueue-adapter:dev
```

## 2. 安装 RBAC 和 adapter

```bash
kubectl apply -f examples/kueue-adapter/rbac.yaml
kubectl apply -f examples/kueue-adapter/deployment.yaml
kubectl -n shardferry-system rollout status deploy/shardferry-kueue-adapter
```

## 3. 应用候选过滤规则

```bash
kubectl apply -f examples/kueue-adapter/preemption-config.yaml
```

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
shardferry.io/preemption-protected=1
shardferry.io/preemption-reason=ProgressThreshold
shardferry.io/training-progress-percent=85
shardferry.io/checkpoint-complete=1
```

## 验收边界

- 本示例验证“Kueue 能看到过滤 label”，不等同于“保护绝对生效”；
- 检查 ClusterQueue 的 classical preemption 设置，避免它绕过过滤规则；
- 真实抢占验收必须观察 Kueue 事件和 victim，而不能只看 label。
