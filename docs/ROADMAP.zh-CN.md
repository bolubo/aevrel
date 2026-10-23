# shardferry 路线图

状态：公开路线图。版本采用 SemVer，候选版本明确标注 pre-release。

## 产品方向

shardferry 不是单一 checkpoint 拷贝工具，而是：

> **scheduler-neutral 的训练状态控制面。**
>
> 把训练运行时和存储里的真实状态，变成调度器、平台和运维可以消费的
> readiness、恢复决策、迁移动作和 proof。

Checkpoint 是第一个完整 vertical，因为它是当前最具体、最可验证、最容易
证明的状态对象。传输是控制面执行的一个动作；Kueue/LRS 是 adapter 或执行
模式，不是新的状态源。

## 当前状态

- 最后一个已发布版本：`0.1.1`；
- 当前候选：`0.2.0-rc.1`，source-only；
- 本地 CPU 路径：`demo`、`demo --dcp`、readiness、restore drill、migrate、
  target load、state bundle；
- Kubernetes 控制面：CRD/controller/agent/ctl 已有本地测试和渲染证据；
- 真实 K8s、跨机、GPU 和大规模验证仍是发布门槛。

## 0.2：控制面基础（当前）

### 用户任务

> 平台能够看到训练任务最近一个可恢复的 checkpoint，验证它真的能加载，
> 在抢占或迁移前知道风险，并在需要时执行一次可证明的移动。

### 交付

- `CheckpointRecord` inventory 和 readiness；
- `RestoreDrill` 真实 load + N step + proof；
- `RescueDecision` 的 restore/restart/migrate/hold；
- static/local-disk migration；
- target-ready 门禁；
- Kueue preemption adapter；
- CLI、CRD、JSON schema 和用户文档；
- `demo --dcp` 本地可复现体验。

### 退出条件

- 本地 CPU 用户路径可复现；
- source 只读、失败不误报成功；
- readiness、drill、rescue、migrate 的接口一致；
- 真实集群/GPU 未跑过的能力明确标为未验证；
- 发布门禁和文档一致。

## 0.3：状态互操作

### 用户任务

> 不同存储、训练运行时和调度器都能把 shardferry 当成中立的训练状态层，
> 而不是绑定到某一个 checkpoint 格式或某一个调度器。

### 方向

- provider 抽象：DCP、对象存储、CSI/VolumeSnapshot、其他 checkpoint；
- scheduler adapter：Kueue、Volcano、Kubeflow 等只消费稳定信号；
- 跨集群、无共享存储的 local-disk inventory 和移动；
- 更完整的 cost/RTO/兼容性模型；
- 更强的 proof、timeline 和失败恢复证据；
- 嵌入模式：library/controller/CRD 可由平台默认集成。

退出条件：

- 至少两个 provider 或两个 consumer 使用同一套契约；
- adapter 不改变核心状态机；
- 新增能力有真实用户路径，不是只有 schema。

## 0.4：在线拓扑迁移（实验）

### 用户任务

> 在训练不停机或尽量少停机的情况下，把部分 rank、shard 或状态从 A 移到 B，
> 并保持全局 step、quorum、coverage 和目标可训练性。

### 方向

- Live Rank Split 的 GPU/NCCL collective 连续性；
- 4 GPU 单节点和 2 节点 A/B 验证；
- 用户可配置 `maxInFlight`；
- source-death takeover、quorum 和 fail-closed；
- 与 static migration 明确分离，失败不静默回退。

这是实验线，不作为 checkpoint 控制面已经成熟的替代品。

## 1.0：可嵌入的训练状态层

长期形态不是“一个偶尔运行的迁移命令”，而是：

> 训练平台、调度器和运维工具默认消费的训练状态契约。

要成为这种基础设施层，需要：

- 稳定的 CRD/API/JSON 契约；
- 可替换的 provider 和 scheduler adapter；
- 可审计的 proof 和失败语义；
- 有真实生产边界和升级策略；
- 不强迫用户绑定单一存储、框架或调度器。

## 不做什么

- 不重写 Kueue/Volcano/Kubeflow；
- 不重写 DCP save/load；
- 不定义新的 checkpoint 格式；
- 不承诺任意 CUDA/Python 堆热迁移；
- 不做通用 Kubernetes 资源浏览器；
- 不做只有监控图表、没有动作和 proof 的“控制面”。

## 优先级原则

1. 先解决真实用户 workflow，再增加 provider/参数；
2. 先保证正确性和 fail-closed，再追求吞吐；
3. 先复用上游实现，再自研无法表达的语义；
4. 只有出现第二个真实 consumer/provider 时才抽象；
5. 未验证的能力必须明确写成实验或待验证。
