# Aevrel 产品路线图

状态：Aevrel 0.3.0 之后的产品路线图，基于第一轮公开需求证据。这里只保留 3 个产品方向，
不承诺版本日期或每版具体功能。

## 先看结论

当前真正值得投入的不是“再做一种传输”，而是把 checkpoint 从文件提升为
可恢复的训练状态，再把这个事实提供给调度器和平台。

三个方向按用户价值排序：

| 排序 | 方向 | 一句话 |
|---:|---|---|
| 1 | Resume-state closure | 证明一个 checkpoint 是否构成完整、可继续训练的恢复状态 |
| 2 | Scheduler / runtime contract | 把 checkpoint/恢复事实变成调度器可以消费的信号 |
| 3 | Checkpoint readiness / verification depth | 让 complete/fresh/loadable/movable 真的可信 |

依赖顺序不是简单的 1→2→3：

```text
3. Readiness/verification 是事实基础
        -> 1. Resume-state closure 是产品主楔子
        -> 2. Scheduler/runtime contract 是平台分发
```

也就是说，**用户价值上 1 最重要；实现上 3 必须先可靠**。

UI/UX、测试覆盖和代码重构属于项目建设，不在这三个产品方向里，见
[项目建设方向](CONSTRUCTION.zh-CN.md)。

## 1. Resume-state closure

### 是什么

Resume-state closure 是“继续训练所需的完整状态闭包”：

```text
model weights
+ optimizer state
+ RNG / sampler
+ dataloader cursor
+ scheduler / epoch / global step
+ TP/PP/DP/EP topology
+ rank-local state
```

它不是“一个 checkpoint 目录里有些文件”，而是：

> 把这些状态搬到另一个 world size、mesh 或集群后，训练能够从同一个逻辑
> step 继续，而不是静默从头开始、丢 dataloader 位置或混用旧 optimizer。

### 解决什么问题

当前社区里，checkpoint 存在、甚至能 load，并不代表能 resume：

- 只保存了 model，没有保存 optimizer 或 dataloader；
- dataloader 第二次恢复时回到开头；
- scheduler 状态没有恢复；
- 新旧 rank 的权重和 optimizer 版本混在一起；
- 训练框架各自实现一套 checkpoint 逻辑，没有中立契约。

公开证据中反复出现这些问题：

- Torchtitan `#5054`：model-only checkpoint 被 auto-resume 选中，缺
  `dataloader.version`；
- Torchtitan `#3907`：第二次连续 resume 后 dataloader 从零开始；
- Torchtitan `#3735`：RL 训练被抢占后回到 step 1，全部进度丢失；
- Ray `#30838`：Tuner restore 不恢复 scheduler state；
- Ray `#67292`：stale snapshot 可能混用新权重和旧 optimizer state。

### 我们做什么

- 定义跨框架的 `ResumeState`/`ResumeClosure` 契约和版本；
- inventory 时识别 model、optimizer、RNG、dataloader、scheduler、
  topology、rank-local state 是否存在；
- restore 前做闭包检查，缺失项必须 fail closed；
- `restore-drill` 验证真实模型/优化器加载和 N 个训练 step；
- 为不同运行时提供 adapter，而不是重写它们的 save/load；
- 在目标 world size/mesh 改变时验证状态映射；
- 把检查结果、缺失项和 proof 提供给 UI、CLI 和调度器。

首个可用切片不需要覆盖所有框架：

```text
DCP/FSDP-like checkpoint
  -> explicit resume manifest
  -> missing-state diagnostics
  -> one real framework adapter
  -> one target-world-size restore proof
```

### 验收

- 能判断一个 checkpoint 是否足以 resume，而不只是是否可 load；
- 缺失 dataloader、scheduler、optimizer 或 RNG 时给出具体原因；
- 至少一个真实训练框架能完成“保存 -> 改变 world size -> 恢复 -> 继续 N step”；
- source 只读，失败不能产生“看起来恢复成功”的状态；
- proof 能绑定 checkpoint version、状态闭包和目标 load 结果；
- 同一套契约能扩展到第二个运行时，而不复制核心状态机。

### 不做什么

- 不定义新的 checkpoint 二进制格式；
- 不重写 DCP、FSDP、Megatron、Ray 的 save/load；
- 不承诺未保存的 RNG/dataloader 也能“完全相同的训练轨迹”；
- 不在第一阶段覆盖任意自定义训练循环。

## 2. Scheduler / runtime contract

### 是什么

这是 Aevrel 提供给调度器和平台的中立状态契约：

```text
这个 workload 最近有没有完整 checkpoint？
checkpoint 有多新？
被抢占后应该 restore 还是 restart？
restore 大概要多久、会丢多少 step？
目标集群/拓扑能不能接？
```

它不是“替 Kueue 调度”，而是补上 Kueue、Volcano、Kubeflow 和训练运行时
之间缺失的事实。

### 解决什么问题

调度器通常只看到 Pod、GPU、优先级和配额，看不到：

- 任务是否刚刚 checkpoint；
- checkpoint 是否完整、是否可恢复；
- 抢占会浪费多少训练进度；
- 重新准入时应该恢复还是从头开始；
- 目标集群是否能接当前 world size/mesh。

公开证据：

- Kueue `#477`（2022 持续到 2026）明确说 Kueue 不知道 checkpoint 是否发生、
  多近、如何 restore；
- Kueue `#7990` 讨论 preemption cost，KEP PR `#8551` 已合并，说明
  “中断成本”正在进入调度层；
- Kubernetes KEP-5710 推进 workload-aware preemption，但 checkpoint/
  interruption cost 仍需要后续设计；
- Volcano `#5998` 提出 checkpoint-aware victim ordering，但没有看到落地实现。

### 我们做什么

- 定义调度器可消费的稳定事实，例如：
  - checkpoint completeness/readiness；
  - checkpoint recency 和估算 RTO/RPO；
  - restore vs restart；
  - target compatibility；
  - interruption cost / progress-loss signal。
- 先做 adapter，不改 Kueue/Volcano/Kubeflow 的核心调度器；
- 让调度器可以选择消费哪些信号，不强迫所有平台使用同一策略；
- 提供 `aevrelctl kueue status|explain` 这类可解释入口；
- 明确区分“事实”（checkpoint 状态）和“策略”（是否抢占、如何加权）。

### 验收

- 一个调度场景能明确回答“现在抢占是否安全、为什么”；
- checkpoint 事实和调度器策略解耦，换调度器不需要重写核心；
- Kueue adapter 至少能展示 checkpoint readiness、restore mode 和 reason；
- 没有 checkpoint 或状态未知时默认保守，不静默当作安全；
- 不依赖 Kueue 才能使用基础 readiness；
- 失败时能解释“缺哪个状态、下一步该做什么”。

### 不做什么

- 不替代 Kueue/Volcano/Kubeflow 的调度器；
- 不把业务权重、配额、公平性全部塞进 aevrel；
- 不做没有证据的自动抢占策略；
- 不假设所有用户都安装 Kueue。

## 3. Checkpoint readiness / verification depth

### 是什么

这是最底层、也是最容易验证的一层：让下面四个判断真的可信：

```text
complete   所有 rank/shard 是否存在且一致
fresh      是否足够新，最大可能丢多少 step
loadable   是否真的能加载，而不是只有 .metadata
movable    目标 world size/mesh/存储是否可接
```

对应状态：

```text
READY | STALE | INCOMPLETE | TARGET_BLOCKED | UNVERIFIED
```

### 解决什么问题

“文件存在”经常被误认为“可以恢复”：

- `.metadata` 存在，但 checkpoint 是 model-only；
- 保存中途崩溃，留下空目录或半写 chunk；
- 所有 rank 文件都在，但 dataloader 或 optimizer 缺失；
- target load 静默挂起，没有超时或明确错误；
- 用户只能看到 DCP 的 opaque error，不知道下一步做什么。

公开证据：

- Torchtitan `#5054`：有 `.metadata` 的 model-only checkpoint 被当成可恢复；
- Torchtitan `#5096`：显式 load step 接受不完整目录；
- Torchtitan `#3065`：1024 GPU 上 DCP load 静默挂起数小时；
- Kubeflow `#2245`：checkpoint/model exporter 长期缺失，用户自己写 Job；
- Ray Data `#65685`：大规模 checkpoint restore 撞内存墙，需要可插拔接口。

### 我们做什么

- 只读 inventory：rank、shard、metadata、checksum、mtime、version；
- 保存完整性检查：空目录、半写、缺 rank、版本不一致；
- 真实 target load / restore drill；
- 把 verification 结果绑定到 checkpoint version 和 metadata digest；
- 变更 checkpoint 后让旧 `loadable=true` 失效；
- 对错误给出可执行原因和下一步；
- 把结果暴露给 CLI、CRD、proof 和 UI。

### 验收

- 不允许只检查文件存在就报告成功；
- 缺失/半写/版本不一致必须 fail closed；
- 真实 drill 必须加载 model/optimizer 并跑 N step；
- `readiness` 在没有真实 drill 时保持 `UNVERIFIED`；
- 多 rank 报告必须覆盖全部 rank，缺一不可；
- checkpoint 变化后旧 proof 不能继续使用；
- 公开命令在无 GPU 环境也可以验证 inventory 和失败路径。

### 不做什么

- 不把 readiness 做成性能 benchmark；
- 不把 checkpoint GC/retention 当作第一阶段的核心；
- 不用文件数量或目录大小替代 load proof；
- 不承诺未验证的跨框架/跨拓扑兼容性。

## 三个方向的关系

```text
Checkpoint readiness / verification
        │  提供可信事实
        ▼
Resume-state closure
        │  提供完整恢复语义
        ▼
Scheduler / runtime contract
        │  提供给平台和调度器
        ▼
平台决策、UI、迁移、LRS
```

传输、LRS、GPU 热切换和 provider 生态仍然可以做，但它们应服务于这条链，
而不是取代这条链。

## 后续验证

这份排序基于第一轮公开 issue/KEP 证据，下一步需要补：

1. 5–10 个真实 AI 平台/训练 infra 用户访谈；
2. 至少两个 local-disk、无共享存储、被抢占的训练团队案例；
3. 跟踪 Kueue KEP-5710 和 preemption cost KEP 的实现；
4. 用最小 resume-state 原型验证 Torchtitan/Ray/FSDP 的跨运行时可行性；
5. 记录反证：如果用户只关心“把 checkpoint 文件搬走”，则方向 1 降级。

详细内部证据和评分不放在公开仓库。
