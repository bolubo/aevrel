# Checkpoint control plane 最小闭环

这套示例只使用 shardferry 自己的 CRD；不依赖 Kueue、Volcano 或任何
调度器插件。

## 1. 扫描 checkpoint

```bash
kubectl apply -f checkpoint-record-selector.yaml
kubectl get checkpointrecords
shardferryctl readiness training-step-100
```

`agentSelector` 会选择所有 ready 的 agent Pod，并行读取每个 agent 的
DCP metadata 与本地 shard 事实，再按 rank 合并。重复 rank 的同一 shard
必须一致；缺失、越界或冲突都会让 `complete=false`。

没有 selector 时，仍可设置 `spec.agentEndpoint` 指向单个 agent。

## 2. 做真实恢复验证

```bash
kubectl apply -f restore-drill.yaml
kubectl get restoredrills training-step-100 -w
shardferryctl readiness training-step-100
```

RestoreDrill 会创建一个 `batch/v1` Indexed Job，强制
`completions=parallelism=targetWorldSize`，并注入 checkpoint 路径、版本、
步数和 report 路径。每个 rank 必须真实加载模型与优化器、运行至少 N step，
通过 `shardferry-target-report` 写回报告，才会得到 `loadable=true`。

控制器不会把“文件存在”或“metadata 可读”当成 load proof。

## 3. 得到 rescue 决策

```bash
kubectl apply -f rescue-decision.yaml
kubectl get rescuedecisions
shardferryctl readiness training-step-100
```

也可以直接用 CLI 创建：

```bash
shardferryctl rescue \
  --event Preempted \
  --workload training \
  --checkpoint training-step-100 \
  --target cluster-b
```

决策规则：

- checkpoint 完整、新鲜、`loadable=true` 且有 drill proof：
  - 无 target：`restore`
  - 有 target 且 `movable=true`：`migrate`
- 缺 checkpoint、未 drill、drill 失败、过期或 target 不可移动：`hold`
- 只有用户显式设置 `fallback: restart` 时，才可能在不可恢复场景返回
  `restart`

决策本身只写 `RescueDecision.status`，不会自动启动第二份训练。这样不会
因为控制面误判产生 split-brain。

## 4. 从 Kubernetes 事件自动生成决策

给训练 Job 加 opt-in：

```yaml
metadata:
  labels:
    shardferry.io/rescue: enabled
  annotations:
    shardferry.io/checkpoint-record: training-step-100
```

`Evicted`、`Preempted`、`BackoffLimitExceeded`、`FailedScheduling`、
`OOMKilled` 等 Warning event 会生成一个 `RescueDecision`。事件 watcher
不依赖任何调度器；Kueue/Volcano 只可提供更丰富的 workload 语义。
