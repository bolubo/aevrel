# Kueue checkpoint-aware preemption guard（0.2 Part 1）

状态：MVP 实现中。CPU 可验证，不依赖 GPU。

## 结论

这一层值得成立，但边界必须窄：

- 它只回答一个二元问题：这个训练 workload 现在是否应该被排除出
  **可配置抢占候选集**；
- 它不计算权重、不替代 priority、不改 quota、不做 admission ordering；
- 它不修改 Kueue 代码，也不假设 Kueue 会理解 shardferry 的自定义 CRD；
- 它输出的是 Kueue 现有 `PreemptionConfig.numericLabels` 能消费的整数 label。

更重要的是，它没有硬件门槛。没有 GPU 也能验证 checkpoint 发布、策略决策、
label 幂等同步和 Kueue 候选过滤。

## 数据流

```text
training Job / checkpoint sidecar
  -> shardferry.io/* checkpoint annotations
  -> shardferry-kueue-adapter
  -> Workload labels
  -> Kueue PreemptionConfig candidate filter
  -> Kueue remains the scheduler
```

adapter 不创建、不删除、不 suspend、不 reprioritize Workload。

## 用户配置

训练 Job 必须显式 opt in：

```yaml
metadata:
  annotations:
    shardferry.io/preemption-protection: "enabled"
    shardferry.io/training-progress-percent: "85"
    shardferry.io/checkpoint-complete: "1"
    shardferry.io/checkpoint-version: "step-8500"
    shardferry.io/checkpoint-age-seconds: "12"
```

字段：

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `preemption-protection` | 是 | `enabled` / `true` / `1` 才处理 |
| `training-progress-percent` | 否 | `0..100`；缺失表示未知 |
| `checkpoint-complete` | 否 | `1/0` 或 `true/false` |
| `checkpoint-version` | 否 | 最近完整 checkpoint 的版本标识；当前仅保留 |
| `checkpoint-age-seconds` | 否 | 最近完整 checkpoint 的年龄；当前仅保留 |

annotations 优先，labels 可作 sidecar 的简化回退。
`checkpoint-version` 和 `checkpoint-age-seconds` 暂不参与二元决策，避免
MVP 偷偷变成 cost scorer。

## 决策表

默认策略：

```text
protect_progress_gte = 0.8
require_checkpoint_complete = true
protect_unknown = true
```

顺序固定：

1. 状态非法且 `protect_unknown=true` -> `protected`，
   reason=`InvalidStateProtected`；
2. 状态未知且 `protect_unknown=true` -> `protected`，
   reason=`UnknownStateProtected`；
3. 状态未知/非法且 `protect_unknown=false` -> `eligible`；
4. 要求 checkpoint 完整但当前不完整 -> `protected`，
   reason=`CheckpointIncomplete`；
5. 进度达到阈值 -> `protected`，reason=`ProgressThreshold`；
6. 其余 -> `eligible`，reason=`Eligible`。

adapter 写入 Workload 的 label：

```text
shardferry.io/preemption-protected=1|0
shardferry.io/preemption-reason=...
shardferry.io/training-progress-percent=...
shardferry.io/checkpoint-complete=1|0
shardferry.io/preemption-decision-source=job|workload
```

## Kueue 接入

需要启用 Kueue 的 `ConfigurablePreemptions` feature gate，并使用：

```text
examples/kueue-adapter/preemption-config.yaml
```

Kueue 配置中需要有：

```yaml
featureGates:
  ConfigurablePreemptions: true
```

核心规则是：

```yaml
numericLabels:
- key: shardferry.io/preemption-protected
  maxValue: 0
  fallbackValue: 0
```

`maxValue: 0` 表示只有 `protected=0` 的 workload 能成为候选。
`fallbackValue: 0` 让没有 label 的普通 workload 仍按 eligible 处理，
避免 adapter 故障时把它们全部排除。

## 已知硬限制

1. **只能过滤，不能排序。** Kueue 的 numeric label 不表达“优先抢占代价最低
   的 workload”。
2. **不是绝对防抢占。** Kueue 会把 classical preemption 候选和
   `PreemptionConfig` 候选做并集。只有 classical 路径也不能选中受保护
   workload 时，保护才是严格的。部署前必须检查 ClusterQueue 的 preemption
   配置；不要对外宣称“打上 label 就一定不会被抢占”。
3. **必须显式 opt in。** 普通 Job 不会被修改，也不会被自动猜测 checkpoint
   状态。
4. **状态可能陈旧。** adapter 默认每 30 秒重读 owner Job；发布端应持续更新
   annotation，而不是只在启动时写一次。
5. **不做 admission。** 如果目标是用 `AdmissionCheck` 在进队列前 delay，
   那是后续 StateGate，不混入本 MVP。

## 本地验证

```bash
go test -count=1 ./internal/kueueadapter
go run ./cmd/kueue-adapter \
  --kubeconfig "$HOME/.kube/config" \
  --workload-api-version v1beta2 \
  --protect-progress-gte 0.8 \
  --require-checkpoint-complete=true \
  --protect-unknown=true
```

kind/Kueue 端到端验收：

1. 创建一个高进度、checkpoint 完整的训练 Job；
2. 创建另一个低进度或 checkpoint 不完整的训练 Job；
3. 触发一个需要抢占的更高优先级 workload；
4. 确认受保护 Workload 的 `preemption-protected` 为 `1`；
5. 确认 Kueue 事件和候选解释中没有把它选为 victim；
6. 删除 adapter 后重新跑一次，确认 `fallbackValue: 0` 的行为符合预期。

在真实 Kueue 集群跑通前，只能声明“策略和 label 同步已验证”，
不能声明“抢占行为已验证”。
