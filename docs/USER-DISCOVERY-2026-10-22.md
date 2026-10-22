# GitHub user discovery — 2026-10-22

状态：第二轮代码/上游验证；仍不是访谈后的用户验证结论

## 方法

先用 GitHub issue 做“问题考古”，目标不是找 star，而是找：

- 真实用户已经遇到的失败；
- 现有 workaround；
- maintainer 明确说“不支持/不在范围”的边界；
- 多个独立项目重复出现的痛点；
- 我们有 K8s controller / migration control-plane 优势的位置。

第一轮覆盖 PyTorch、Kueue、Kubeflow Trainer、DeepSpeed、Ray、Volcano。
GitHub 搜索 API 触发过 secondary rate limit，因此这是首轮而非穷举。

## 第二轮：源码验证结论

### 1. Kueue checkpoint-aware preemption

**代码结论：成立为“控制面缺口”，但不是完整的现成产品接口。**

- Kueue 当前已有 `PreemptionConfig`，支持按 Workload 的 numeric label
  选择抢占候选；这意味着 checkpoint freshness/preemption cost 可以先通过
  外部 controller 发布 label 的方式接入，不必等新 API。
- 但 Kueue 的调度代码没有 checkpoint state、last checkpoint 或
  restore-vs-restart 字段；`PreemptionConfig` 只解决“如何筛候选”，
  不解决“最后 checkpoint 是否完整、应该怎么恢复”。
- KEP-5710 的 non-goal 明确包含 rescheduling；它主要定义 PodGroup/workload
  级抢占，不等于 checkpoint-aware restore。
- Kueue #477 仍然 open；2026 的评论明确提到 WG Checkpoint/Restore、
  KEP-5710 和“restore from checkpoint vs start fresh”的问题。

**判断**

这是一个值得做控制面实验的方向，尤其是“checkpoint freshness 标签 +
PreemptionConfig”可以在不修改 Kueue 核心的情况下先做 PoC。但它还不能
证明用户愿意安装 shardferry；需要真实 Kueue 用户访谈。

### 2. PyTorch separate DeviceMesh local-shard migration

**代码结论：强烈成立，是当前最明确的跨域空隙。**

- PyTorch DCP 默认 planner 的 resharding 仍然通过 checkpoint storage
  读取多个 shard；它假设 source/target 能访问同一个 checkpoint 位置。
- 新出现的 `PGTransport` / `_ReplicationStager` 用 process group 在同一组
  rank 间做 checkpoint 复制；它要求同一个 process group，且复制逻辑假设
  world size 为偶数，不解决两个分离 DeviceMesh 或跨集群。
- #154787 的用户原话是“frequently move and reshard local sharded
  checkpoints from a set of machines to another set of machines”，并明确
  说 DCP 依赖 NFS/S3，nvRX local checkpoint 不支持 reshard。
- maintainer 也指出两个独立 DeviceMesh、特别是不同 world size 是困难场景。

**判断**

这是 shardferry 最硬的技术位置，但它可能是一个相对窄的用户群；必须找到
至少 3 个真的在 production 里反复做这件事的团队，而不是只有一个 issue
author。

### 3. Kubeflow Trainer checkpoint exporter / target-ready

**代码结论：成立为平台集成缺口，不是独立数据面护城河。**

- Kubeflow Trainer 当前代码支持 TrainJob suspend/resume 和 TorchTune
  checkpointer 配置，但没有通用的 checkpoint exporter/import API。
- #2245 的讨论明确需要 sidecar/exporter、PVC/S3/Model Registry 和
  checkpoint 生命周期；#2777 正在推进 CRIU/cuda-checkpoint 的透明恢复，
  这是相邻但不同的路线。
- 这类能力更像平台应该自己拥有；shardferry 更适合提供 target-ready、
  checkpoint state、migration transaction 和 proof，而不是重写 exporter。

**判断**

可成立，但优先级低于前两项。它适合作为分发/集成入口，不适合作为 0.2 的
唯一核心。

### 暂不进入 top 3：DeepSpeed universal checkpoint

#5405 证明“不同 GPU 数恢复”是真实痛点，但当前项目的 DCP vertical 不应
为了一个 provider 扩成通用 checkpoint 平台。它进入后续 provider backlog。

## 线索优先级

### A. Checkpoint-aware preemption / restore

**证据**

- Kueue #477: [Use information about last checkpoint on preemption](https://github.com/kubernetes-sigs/kueue/issues/477)
- Kueue #7990: [consider preemption cost](https://github.com/kubernetes-sigs/kueue/issues/7990)
- Kubernetes enhancement #5710: [Workload-aware preemption](https://github.com/kubernetes/enhancements/issues/5710)

**用户问题**

平台知道 workload 会被抢占，但不知道：

- 它是否刚 checkpoint；
- 最后一个完整 checkpoint/step 在哪；
- 抢占后应该 restore 还是 fresh start；
- 抢占窗口内应该给多久完成 checkpoint；
- 怎样按“中断成本”选择抢占受害者。

**现有状态**

Kueue 已经 suspend 而不是删除 workload；应用自己负责 checkpoint 和恢复协调。
Kueue #477 在 2026 被重新激活，并明确关联 WG Checkpoint/Restore
和 KEP-5710。

**为什么适合 shardferry**

这是控制面和状态契约，不是“再写一个复制器”。它直接需要：

- checkpoint state/freshness；
- restore/restart decision；
- migration/placement primitive；
- status、proof、失败分类。

它比 Live Handoff 更贴近 Kueue/Kubernetes 的真实工作流，也更符合我们的
controller/platform 背景。

**风险**

可能主要落在 Kueue/Kubernetes 上游 API；如果 shardferry 只是一个
“提供 checkpoint 状态的 controller”，必须证明自己能补上跨域移动和
target readiness，而不只是加 annotation。

### B. Separate DeviceMesh + local-shard migration

**证据**

- PyTorch #154787: [Redistribute DTensor across different DeviceMeshes](https://github.com/pytorch/pytorch/issues/154787)

**用户原话摘要**

用户需要频繁把本地分片 checkpoint 从一组机器搬到另一组机器，目标
DeviceMesh 与源 DeviceMesh 不同；DCP 假设有全局 checkpoint 目录（NFS/S3），
而 nvRX local checkpoint 不支持 resharding；用户希望保留本地并行写入，
恢复时再做重分片。

**maintainer 信号**

- DCP 能做共享存储场景的 load-time reshard；
- 两个分离 DeviceMesh 的迁移不是 SPMD，实现有挑战；
- 同 world size 是简单情况，不同 world size 是困难情况。

**为什么适合 shardferry**

这是我们当前 0.1 的最强技术匹配：source/target 不共享全局目录、local
shards、不同 DeviceMesh、需要 range-level plan 和 target load。

**风险**

- 如果 upstream DCP 吸收跨 mesh transport，我们的核心会被下压；
- 需要证明迁移发生在实际生产环境，而不是一个 issue author 的孤立实验；
- 需要跨版本、跨框架兼容性，而不是只支持我们自己的 toy DCP。

### C. Kubeflow Trainer checkpoint exporter / restore

**证据**

- Kubeflow Trainer #2245: [model exporter for checkpointing and training output](https://github.com/kubeflow/trainer/issues/2245)
- Kubeflow Trainer #2777: [transparent GPU checkpointing with CRIU](https://github.com/kubeflow/trainer/issues/2777)

**用户问题**

训练平台有 Job、PVC、S3、Model Registry，但缺少一个明确的
checkpoint/export/restore 生命周期组件。CRIU 方案在解决进程级透明恢复，
但 topological resharding 仍然需要 DCP/DeepSpeed 这类状态层。

**机会**

不一定由 shardferry 单独做 exporter；可以把 exporter、CRIU、DCP
作为 provider，shardferry 提供 target-ready、migration transaction 和
proof。Kubeflow 是分发和集成入口，不是要重写的平台。

### D. DeepSpeed different-GPU resume / universal checkpoint

**证据**

- DeepSpeed #5405: [resume with different GPU number](https://github.com/deepspeedai/DeepSpeed/issues/5405)

**用户问题**

用户从 2×2 GPU 换到 1×4 GPU 恢复时失败；maintainer 说明 ZeRO3
universal checkpoint 尚未支持，ZeRO2 和 ZeRO3 checkpoint 也不兼容。

**机会与边界**

这是真实痛点，但不是我们当前 DCP 的第一 vertical。更合理的策略是未来把
DeepSpeed Universal Checkpoint 作为 provider，而不是现在扩成
“所有框架都支持”。

## 当前综合判断

第二轮验证后的三个方向是：

1. **Kueue/Kubernetes checkpoint-aware lifecycle**：控制面、抢占、restore
   decision、checkpoint state contract；成立，但需要用户/Kueue 集成验证。
2. **PyTorch DCP separate-DeviceMesh local-shard mobility**：数据面、residency、
   reshard、target-ready；技术空隙最明确。
3. **Kubeflow Trainer target-ready/exporter integration**：平台入口，不是核心
   数据面；适合作为分发和后续 provider。

前两项组合起来，比单独的 Live Handoff 更像真实平台需求：

> scheduler knows a workload needs to move;
> platform needs to know whether the checkpoint is viable;
> target needs the right topology and proof;
> source/target may not share storage.

Live Handoff 仍然可能是这个组合的一个高阶形态，但不应在没有用户验证前
单独成为 0.2 的唯一承诺。

## 下一步

- 对 issue authors / maintainers 做 5–8 次问题访谈，不问“会不会用”，只问
  最近一次真实迁移/恢复；
- 验证至少 3 个团队是否真的有“无共享存储 + 异拓扑 + 可安装 controller”
  的组合；
- 为 Kueue #477/KEP-5710 与 PyTorch #154787 各写一张完整 problem card；
- 如果两条线都成立，定义一个 **CheckpointState + MobilityPlan +
  MobilityTransaction + Proof** 的最小控制面契约，再决定 0.2 是
  checkpoint-aware preemption、local-shard migration，还是两者合并；
- 如果只有 one-off 需求，保持 0.1.x 工具定位，不把 Live Handoff 硬做成平台。
