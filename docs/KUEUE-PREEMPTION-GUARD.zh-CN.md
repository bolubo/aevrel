# Kueue checkpoint-aware preemption guard（0.2 Part 1）

状态：MVP 实现中。CPU 可验证，不依赖 GPU。

## 结论

这一层值得成立，但边界必须窄：

- 它只回答一个二元问题：这个训练 workload 现在是否应该被排除出
  **可配置抢占候选集**；
- 它不计算权重、不替代 priority、不改 quota、不做 admission ordering；
- 它不修改 Kueue 代码，也不假设 Kueue 会理解 aevrel 的自定义 CRD；
- 它输出的是 Kueue 现有 `PreemptionConfig.numericLabels` 能消费的整数 label。
- 默认观测源是 aevrel 的 `CheckpointRecord`；没有 record 时才回退到
  Job/Workload annotations。

更重要的是，它没有硬件门槛。没有 GPU 也能验证 checkpoint 发布、策略决策、
label 幂等同步和 Kueue 候选过滤。

## 数据流

```text
training Job / checkpoint sidecar
  -> aevrel.dev/* checkpoint annotations
  -> aevrel-kueue-adapter
  -> Workload labels
  -> Kueue PreemptionConfig candidate filter
  -> Kueue remains the scheduler
```

adapter 不创建、不删除、不 suspend、不 reprioritize Workload。

## 用户配置

训练 Job 必须显式 opt in；有 `CheckpointRecord` 时 state 以控制面为准：

```yaml
metadata:
  annotations:
    aevrel.dev/preemption-protection: "enabled"
    aevrel.dev/training-progress-percent: "85"
    aevrel.dev/checkpoint-complete: "1"
    aevrel.dev/checkpoint-version: "step-8500"
    aevrel.dev/checkpoint-age-seconds: "12"
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
`aevrel.dev/checkpoint-record` 可显式指定 record；没有显式引用时，adapter
按 Workload 的 owner Job 查找 `CheckpointRecord.spec.jobRef`，多个匹配按
step/mtime 选最新，完全无法区分时 fail closed。只有 readiness 为 `READY`
（complete/fresh/loadable）才视为可抢占；未验证、过期或缺失 record 默认
protected。
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
aevrel.dev/preemption-protected=1|0
aevrel.dev/preemption-reason=...
aevrel.dev/training-progress-percent=...
aevrel.dev/checkpoint-complete=1|0
aevrel.dev/preemption-decision-source=job|workload
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
- key: aevrel.dev/preemption-protected
  maxValue: 0
  fallbackValue: 1
```

`maxValue: 0` 表示只有 `protected=0` 的 workload 能成为候选。
strict profile 使用 `fallbackValue: 1`：没有 label 的 workload 默认按
protected 处理，只有 adapter 显式发布 `eligible` 才能成为 victim。
这是 fail-safe，不是 fail-open；它也避免 preemptor 自己因为缺 label 被
选中。试运行可使用 `preemption-config-advisory.yaml` 的
`fallbackValue: 0`，但那不提供二元保护。

## 已知硬限制

1. **只能过滤，不能排序。** Kueue 的 numeric label 不表达“优先抢占代价最低
   的 workload”。
2. **不是绝对防抢占。** Kueue 先走 classical preemption；只有 classical
   候选不足以满足 preemptor 时，才会继续追加 `PreemptionConfig` 候选。
   因此 label 只能约束 configurable 阶段，约束不了 classical 阶段。只有
   classical 路径也不能选中受保护 workload 时，保护才是严格的。部署前必须
   检查 ClusterQueue 的 preemption 配置；不要对外宣称“打上 label 就一定
   不会被抢占”。
3. **必须显式 opt in。** 普通 Job 不会被修改，也不会被自动猜测 checkpoint
   状态。
4. **状态可能陈旧。** adapter 默认每 30 秒重读 owner Job；发布端应持续更新
   annotation，而不是只在启动时写一次。
5. **不做 admission。** 如果目标是用 `AdmissionCheck` 在进队列前 delay，
   那是后续 StateGate，不混入本 MVP。

### strict profile 的精确条件

在 Kueue 当前实现中，configurable 候选是在 classical 候选失败后才追加的，
不是提前做交集。因此严格保护必须同时满足：

1. ClusterQueue 的 `withinClusterQueue=Never`；
2. `reclaimWithinCohort=Never`；
3. `borrowWithinCohort.policy=Never`；
4. FairSharing 不参与抢占；
5. ClusterQueue 通过 `kueue.x-k8s.io/preemption-config-name` 引用
   `PreemptionConfig`；
6. `PreemptionConfig` 对 `preemption-protected` 使用 `maxValue: 0`；
7. strict profile 的 `fallbackValue=1`，让未知状态默认不被抢占。

缺少任一条，二元保护就退化为 advisory：仍然能过滤一部分候选，但不能
承诺“protected workload 永远不会被 Kueue 选中”。

## 安装与产品入口

推荐通过 aevrel Helm chart 安装 adapter、RBAC 和默认
`PreemptionConfig`：

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

然后给目标 ClusterQueue 加 annotation：

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

用户不需要直接读 Workload YAML：

```bash
aevrelctl kueue status
aevrelctl kueue explain <workload>
```

输出会给出 `Protected`、`Reason`、progress/checkpoint/source 和 Kueue
condition，并在最后给出下一步检查方向。

adapter 以 `--strict=true` 启动时，会在启动阶段检查
`PreemptionConfig` 的 strict numeric-label 规则，以及引用它的 ClusterQueue
的 classical 三项配置；检查失败就退出，不启动控制器。FairSharing 是 Kueue
的全局配置，adapter 无法从单个 ClusterQueue 推断，仍需平台侧关闭。

## 适配器抽象

adapter 只应固定四个边界：

```text
ObservationSource  -> 读取 checkpoint 状态（当前是 Job annotations）
Policy             -> 纯函数，输出 protected/eligible
Projector          -> 把 Decision 投影成 Workload labels
Kueue Profile      -> 外部的 PreemptionConfig + ClusterQueue 配置
```

不要把 Kueue 的 classical/fair-sharing 算法复制进 adapter，也不要维护第二个
调度队列。这样以后接 sidecar、外部 checkpoint provider 或别的调度器时，只需要
替换 `ObservationSource`/`Projector`，策略本身不变。

## 本地验证

```bash
go test -count=1 ./internal/kueueadapter
go run ./cmd/kueue-adapter \
  --kubeconfig "$HOME/.kube/config" \
  --strict=true \
  --preemption-config-name=checkpoint-guard \
  --observation-source=auto \
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
6. 删除 adapter 后重新跑一次，确认 strict profile 下没有新 label 的
   Workload 仍按 `fallbackValue: 1` 被排除；如果使用 advisory profile，
   则确认它按 `fallbackValue: 0` 仍可被抢占。

没有真实集群证据时只能声明“策略和 label 同步已验证”。下面的 CPU-only
kind 验证已经覆盖抢占 victim 选择；生产 Kueue 版本、GPU 场景和规模行为仍需
单独验证。

## 2026-10-22 kind 实测

使用 Kueue `main`、`ConfigurablePreemptions=true`、上面的 strict setup
跑过一次 CPU-only 场景：

- `protected-train`：`preemption-protected=1`，保持 `Admitted` 和 Pod
  `Running`；
- `eligible-train`：`preemption-protected=0`，被
  `ConfigurablePreemption` 抢占；
- `high-priority-preemptor`：没有 label，按 `fallbackValue: 1` 默认不成为
  victim，正常获得 quota；
- 放开 strict 条件、把 fallback 改为 `0` 时，preemptor 自己也可能进入候选，
  因此 strict 不采用 fail-open。

可复现步骤在
[`examples/kueue-adapter/kind-e2e`](../examples/kueue-adapter/kind-e2e/README.zh-CN.md)。
