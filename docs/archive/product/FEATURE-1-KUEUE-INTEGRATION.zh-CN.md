# Feature 1：Kueue checkpoint-aware preemption 怎么做

状态：实现设计卡

## 先说结论

我们不能定义一个 `CheckpointState` CRD，然后假设 Kueue 会遵守。Kueue 不会
自动读取自定义语义。

Feature 1 分两层：

1. **现在就能做的 PoC**：shardferry 发布 checkpoint freshness/cost 的
   numeric label；Kueue 通过现有 `PreemptionConfig.numericLabels` 做候选
   过滤。
2. **真正 cost-aware 的选择**：需要 Kueue 上游把 checkpoint cost 纳入
   candidate ordering，或者在 Kueue 中增加正式字段。不能靠 fork 硬改后
   宣称兼容。

## Kueue 当前事实

基于 `kubernetes-sigs/kueue` 当前源码：

- `PreemptionConfig` 是 alpha API，ClusterQueue 通过 annotation
  `kueue.x-k8s.io/preemption-config-name` 引用。
- `PreemptionConfigNumericLabelConstraint` 支持 `minValue`、`maxValue`、
  `comparison` 和 `fallbackValue`，会根据 Workload label 过滤候选。
- numeric label filter 只决定“这个 candidate 是否 eligible”；
  `PreemptionEvaluator.orderedCandidates` 最终仍按 Kueue 的
  classical/fair-sharing candidate ordering 排序。
- Job/JobSet 的 label 不会自动进入 Workload，必须在 Kueue
  `integrations.labelKeysToCopy` 中配置。
- Kueue 代码中没有 checkpoint state、last checkpoint 或
  restore-vs-restart 的正式字段。

因此：

> 现有 API 可以阻止 Kueue 抢占“没有 checkpoint 的任务”，  
> 但不能自动让它选择“checkpoint 代价最低的任务”。

## 中间层：shardferry-kueue-adapter

不修改 Kueue 源码，单独部署一个 Go controller：

```text
training Job / checkpoint sidecar
        -> CheckpointState / labels
        -> shardferry-kueue-adapter
        -> Workload labels
        -> Kueue PreemptionConfig (existing API)
        -> Kueue selects eligible victims
        -> adapter observes Preempted/Evicted condition
        -> CheckpointMigration -> target Job
```

### 组件职责

1. **checkpoint publisher**：训练 sidecar/source agent 写 checkpoint
   complete、version、step、freshness、cost。
2. **kueue adapter**：watch Workload/Job/Pod，维护 Workload 的
   `shardferry.io/checkpoint-*` labels；只 patch 公开对象，不改 Kueue。
3. **policy materializer**：生成/校验 `PreemptionConfig` 和 Kueue
   `integrations.labelKeysToCopy`，也可以只输出待平台审批的 YAML。
4. **preemption observer**：watch `Workload.status` 的
   `Preempted`/`Evicted`，创建迁移任务。
5. **target launcher**：target Job/TrainJob 加载并运行 N step；只有成功
   才写 target-ready。

## 更通用的前置层：StateGate

如果目标是“先经过 shardferry 过滤，再进 Kueue”，不要让 shardferry 维护
第二个作业队列，也不要假装重写 Kueue 的 admission。应该使用 Kueue 已有的
AdmissionCheck 作为前置 gate：

```text
training Job
   -> shardferry StateGate
        - checkpoint capability?
        - recoverability?
        - policy: admit / delay / reject
   -> Kueue AdmissionCheckState = Ready / Pending / Rejected
   -> Kueue quota + priority + fair-sharing
```

这要求 ClusterQueue 显式引用 shardferry AdmissionCheck。对普通非训练 Job，
可以配置为直接 Ready，不改变原有调度行为。

### StateGate 应该输出的不是“权重”，而是决策

```text
Decision:
  Admit
  DelayUntilCheckpoint
  Reject
Cost:
  interruption cost / recovery cost
Recovery:
  restart | restore | migrate
```

调度器可以只用 Decision 做过滤，也可以在有 cost-aware 能力时使用 Cost 排序。
这样不同调度器不需要理解 DCP，只需要消费同一套决策。

### 前置层不是第二个调度器

不要做：

- 自己维护 pending job 队列再去创建 Kueue Workload；
- 自己计算 quota、fair share、gang scheduling；
- 直接 evict Kueue 的 victim；
- 用一个自定义 CRD 冒充调度器状态。

这些会制造双状态源、竞态和不可解释的调度结果。前置层只负责“状态是否允许
进入/触发资源调度”，资源如何分配仍归 Kueue。

### 不能把 PreemptionGate 当成 victim 保护

Kueue 的 `spec.preemptionGates` 是控制**preemptor 能不能触发抢占**，
不是“保护某个 victim 不被抢占”。它能做的是：

- 给高优先级 preemptor 加一个 closed gate；
- adapter 在 target 准备好后 open；
- 但如果没有改变候选标签，Kueue 仍会按自己的 candidate ordering 选 victim。

只有在 `ConcurrentAdmission`/`MultiKueueOrchestratedPreemption` 等 feature
gate 启用时，gate 才生效。它是可选的协调手段，不是核心保证。

### 中间层的核心保证

- Kueue 继续负责调度、配额、抢占；
- adapter 不替换 Kueue，不伪造 priority；
- Kueue 不可用时 adapter 退出，原有调度不受影响；
- shardferry 的迁移是 preemption 之后的可选动作，不是抢占的前置条件。

## PoC：不 fork Kueue

### 1. 训练侧发布 checkpoint 信息

由 shardferry checkpoint agent（或训练 Job 的 sidecar）写入一个可复制的
integer label，例如：

```text
shardferry.io/checkpoint-complete=1
shardferry.io/checkpoint-age-seconds=42
shardferry.io/checkpoint-cost=7
```

- `checkpoint-complete`：1 表示最近一个 version 完整；
- `checkpoint-age-seconds`： freshness；
- `checkpoint-cost`：按 checkpoint interval、剩余训练时间、迁移成本算出
  的 0–100 整数；
- 缺失或不完整时不能伪造 fallback。

### 2. Kueue 配置

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
integrations:
  labelKeysToCopy:
  - shardferry.io/checkpoint-complete
  - shardferry.io/checkpoint-age-seconds
  - shardferry.io/checkpoint-cost
```

### 3. PreemptionConfig 规则

```yaml
apiVersion: kueue.x-k8s.io/v1alpha1
kind: PreemptionConfig
metadata:
  name: checkpoint-aware
spec:
  rules:
  - name: only-preempt-cheap-checkpoints
    activationPolicy:
      trigger: Always
    candidateSelectors:
    - scope: WithinClusterQueue
      numericLabels:
      - key: shardferry.io/checkpoint-complete
        minValue: 1
      - key: shardferry.io/checkpoint-cost
        maxValue: 30
```

这能表达“只抢占 checkpoint 完整且代价低于阈值的任务”。

它不能表达“在所有候选里挑 cost 最低的任务”，因为 numericLabels 只过滤，
不排序。

### 4. 状态同步

Kueue 的 `labelKeysToCopy` 主要发生在 Workload 创建时。checkpoint 状态是
动态变化的，所以需要：

- 训练 Pod sidecar 写 `CheckpointState`；
- shardferry controller 将状态同步到 Workload label；
- 确认 Kueue 不会在下一次 reconcile 覆盖该 label；
- 如果会被覆盖，改为在 Job label + Workload recreation 时携带，或者走
  upstream 正式字段。

第一步先用 patch Workload 做 PoC，把“是否会被覆盖”作为验收项。

## 真正 cost-aware：必须 upstream

如果产品要保证 Kueue 按 checkpoint cost 排序，而不是只过滤，需要上游做
至少一项：

1. `Workload` 增加 checkpoint state/cost 字段；
2. Kueue 的 candidate ordering 把 checkpoint cost 作为排序因子；
3. 或引入可插拔 preemption policy，让外部 controller 提供排序。

这不是“shardferry 自己写一个 CRD”能解决的。我们的选择：

- 先做 PoC，证明阈值过滤有价值；
- 用 KEP/issue 和真实用户数据推动 upstream；
- 不维护一个长期 Kueue fork；
- 如果 upstream 不接受，就把 feature 1 降级为 optional adapter，不把它
  当成 0.2 的唯一卖点。

## 0.2 PoC 验收

- Kueue `ConfigurablePreemptions` 在 kind 中启用；
- 两个训练 Workload，一个有完整 checkpoint，一个没有；
- 高优先级 preemptor 只能选中 checkpoint 完整的那个；
- checkpoint 过期/不完整时不会被误判；
- 事件和 `Workload.status` 能解释为什么选中/未选中；
- 不改 Kueue 代码即可跑通；
- 记录“只能过滤、不能排序”的已知限制。

## 不做什么

- 不改 Kueue 内部优先级来伪装成本；
- 不写一个自认为会被 Kueue 读取的 CRD；
- 不把 custom label 当成 stable upstream API；
- 不宣称 cost-aware preemption 已经完成，直到 Kueue 真正排序。
