# Checkpoint 生态与用户需求调研

状态：调研结论（2026-10-22）

## 0. 结论

checkpoint 是根问题，迁移只是 checkpoint 生命周期中的一个动作。

但“checkpoint 重要”不等于“再做一个 checkpoint 库”：

- save/load、async、resharding、fault tolerance、retention 已有强项目；
- 相对空白的是跨框架、跨控制域、可证明的 checkpoint readiness；
- 迁移应作为 readiness 的一个动作，而不是产品中心。

## 1. 用户需要什么

| 层 | 用户问题 | 代表能力 |
|---|---|---|
| 保存 | 不要为了 checkpoint 停太久 | async save、pinned memory、D2H overlap |
| 加载 | 目标拓扑能不能继续 | load-time reshard、model/optimizer load、state closure |
| 生命周期 | 保留几份、何时删、占多少盘 | retention、GC、storage policy |
| 故障恢复 | rank/节点挂了怎么继续 | quorum、elastic、takeover、restart |
| 可恢复性 | 最近一次真的能恢复吗 | restore drill、target load、proof |
| 迁移 | A 有 B 缺，怎么搬过去 | local-disk inventory、transport、commit |

用户真实问题通常是：

- 最新完整 checkpoint 在哪？
- 这份 checkpoint 能不能在 target 卡数上加载？
- 抢占会丢多少步？
- 为什么 target load 又失败？
- 旧 checkpoint 能不能安全删？

## 2. 现有项目

### PyTorch DCP / TorchTitan

PyTorch DCP 提供 save/load、metadata 和 load-time resharding。TorchTitan
的 `CheckpointManager` 提供 interval、`keep_latest_k`、async、HF/DCP
转换和 retention。

结论：单训练作业内的 checkpoint manager 已成熟，但不提供 fleet inventory、
target readiness 或跨控制域事务。

### ByteCheckpoint

ByteCheckpoint 提供 framework-agnostic save/load、load-time resharding、
async/parallel I/O、pinned memory、merge/convert/inspect，建议使用 POSIX
共享文件系统。

结论：数据面和重分片是强竞品；不解决“哪份 checkpoint 可用”、
“target 是否真的能继续训练”和跨控制域证明。

### NVIDIA NVRx

NVRx 提供 async checkpoint、local checkpoint、fault tolerance、hang
detection、coordinated worker restart、straggler detection。

结论：最接近 checkpoint 运维的相邻竞品，但定位是 NVIDIA/Megatron
训练栈的 resiliency extension，不是中立 Kubernetes 状态层。

### Orbax

Orbax 是 JAX 的 checkpoint manager，支持 async、retention、灵活 storage。
它不覆盖 PyTorch DCP、local-disk shard、Kubernetes、跨控制域。

### Determined AI / Ray Train

Determined 有 checkpoint storage、GC、API 和平台内恢复；Ray Train 有
`Checkpoint`/`CheckpointConfig`/`Result` 和平台内失败恢复。

结论：平台内 checkpoint 管理已有成熟替代；它们是完整平台，不是中立
迁移/readiness 层。

### TorchFT / DeepSpeed

TorchFT 提供 per-step fault tolerance、quorum 和 live recovery；DeepSpeed
提供 Universal Checkpoint 和不同 GPU 数恢复。

它们解决“训练过程中怎么继续”，不回答“这份 checkpoint 现在是否
可恢复/可迁移”。

### Kueue / Volcano / Kubeflow

- Kueue `#477` 仍 open：需要 last checkpoint、freshness、restore vs restart；
- Volcano `#5998` 讨论 checkpoint-aware victim ordering，方案只读 Pod
  annotation，以 “Discussed offline” 关闭；
- Kubeflow Trainer `#2245` 仍在做 exporter/checkpoint lifecycle；
  `#2777` 是 CRIU，不是跨拓扑状态迁移。

结论：这些是消费者/集成点，不是统一 checkpoint state plane。

## 3. 竞争矩阵

| 项目 | 保存/加载 | 重分片 | async | 故障恢复 | retention | 多控制域 | 可恢复证明 |
|---|---|---|---|---|---|---|---|
| PyTorch DCP | 强 | 强* | 基础 | 弱 | 弱 | 否 | 弱 |
| TorchTitan | 强 | 强 | 强 | 中 | 强 | 训练栈内 | 弱 |
| ByteCheckpoint | 强 | 强 | 强 | 弱 | 中 | 多框架 | 弱 |
| NVRx | 强 | 中 | 强 | 强 | 中 | NVIDIA 栈 | 中 |
| Orbax | 强 | JAX 内 | 强 | 弱 | 强 | JAX | 弱 |
| Determined | 强 | 平台内 | 中 | 强 | 强 | 平台绑定 | 中 |
| Ray Train | 强 | 平台内 | 中 | 强 | 中 | Ray 绑定 | 中 |
| TorchFT | 中 | 弱 | — | 强 | 弱 | PyTorch FT | 中 |
| Kueue/Volcano/Kubeflow | 弱 | 弱 | — | 事件侧 | 弱 | 调度器 | 弱 |

`*` DCP resharding 依赖可访问的 checkpoint storage。

## 4. 真实需求信号

- Kueue `#477`：调度器缺少 checkpoint 状态和 restore decision。
- Volcano `#5998`：抢占成本需要 checkpoint recency，但方案只是 annotation。
- Kubeflow `#2245`：checkpoint exporter/lifecycle 仍是开放需求。
- TorchFT `#329`：用户报告恢复接近 948 秒。
- DeepSpeed `#8884`/`#8874`：universal checkpoint resume 仍有真实 bug。
- ByteCheckpoint `#7`：4-GPU checkpoint 在单 GPU 加载失败。
- TorchTitan 把 `keep_latest_k`、async、HF 转换做成正式配置。

这些证据支持“checkpoint 需求真实”，也说明单点 save/load/reshard 已有
强手。

## 5. 我们做什么

### 应该做

1. CheckpointReadiness：complete/fresh/loadable/movable。
2. 只读 inventory：按 job/rank 发现 local-disk shard。
3. Restore drill：真实 target load + optimizer + N step。
4. Provider 集成：DCP/ByteCheckpoint/NVRx/TorchTitan/TorchFT 作为后端。
5. 跨控制域 mobility：readiness 判定 movable 后执行事务性迁移。
6. Rescue decision：restore/restart/migrate/hold。
7. Proof/audit：source digest、target load、coverage、timeline。

### 不应该做

- 重写通用 checkpoint save/load 库；
- 和 DCP/ByteCheckpoint 比单机吞吐；
- 只做 checkpoint dashboard；
- 重写 Kueue/Volcano/Kubeflow 调度器；
- 把迁移当成产品中心；
- 用文件存在冒充可以恢复；
- 用单框架 manager 冒充 fleet control plane。

## 6. 产品落点

```text
CheckpointReadiness
  -> complete / fresh / loadable / movable
  -> rescue decision
  -> provider-backed restore/load
  -> mobility transaction
  -> proof
```

常驻 agent + controller 维护状态和决策；restore drill、migration、LRS
worker 按需执行。Kueue/Volcano/Kubeflow 只是 adapter；DCP/ByteCheckpoint/
NVRx 是 provider，不是产品身份。

## 7. 内部验收计划

不依赖外部访谈；用可重复环境验证 readiness 是否真实成立：

1. 构造 1000 个逻辑 checkpoint，覆盖完整、缺 rank、重复 rank、截断、
   不可读、版本不一致、target 不兼容；
2. 本地/CI 跑 readiness 查询和 source digest 回归；
3. kind 跑 agent/controller 重启、selector 发现和 target-ready；
4. 用 4 GPU/2 节点验证 mobility/LRS；
5. 用故障矩阵验证 fail closed 和 proof。

成立条件：

- readiness 与 ground truth 100% 一致；
- 失败路径不误报成功；
- 用户不需要读内部 journal 才能知道下一步；
- 关键状态、proof 和 timeline 可复核。
