# shardferry 路线图

## 版本规则

路线图按 SemVer 编号：

- **0.1.x**：静态 checkpoint 迁移的维护线；
- **0.1.2**：Rescue Demo，做出可传播、可复现的第一次体验；
- **0.1.3**：迁移性能专项，消掉当前可优化的 I/O 和实现开销；
- **0.2.0**：Checkpoint Mobility Control Plane；
- 口语中的“0.11/0.12/0.13”统一写作 `0.1.1/0.1.2/0.1.3`，都属于 0.1 维护线。

2026-10-22 owner 决策：先做 `0.2.0-rc.1` 产品闭环（Mobility、CLI、target-ready、
诊断与文档），GPU 验证通过后切 `0.2.0`。`0.1.2` Rescue Demo 和 `0.1.3`
性能专项保留为候选后的体验/性能工作，不再阻塞 `0.2.0-rc.1`。
产品长期形态和日常 readiness 价值见
[产品形态与日常价值](PRODUCT-FORM.zh-CN.md)。

大版本的门槛不是“功能变多”，而是用户完成任务的形态发生变化。

## 0.1：静态 checkpoint 迁移（已完成，维护中）

### 用户任务

> 一个已经结束的训练任务留下了 local-disk DCP；把它搬到目标集群/目标
> world size，并证明目标训练可以加载。

### 已发布能力

- `0.1.0`：DCP 静态迁移 preview；
- `0.1.1`：GPU-validated alpha；
- 本地 `demo` / `doctor` / `migrate`；
- DCP range read、无 full gather、target DCP export；
- bitwise verify、journal resume、all-or-nothing commit；
- Kubernetes `CheckpointMigration` controller + source/target agents；
- selector 发现、preflight、plan preview、rank-level describe；
- 跨集群 gateway、per-cluster token、mTLS；
- proof、support bundle、release wheel 和 Helm chart；
- 4×L20 上 `4→2/4→3/4→1/1→4` fp32 和 `4→2` bf16；
- 769 MiB `4→3` 真实 GPU 数据；
- 两套 kind 的 16→8、synthetic 128→64 和 1 GiB 数据链路。

### 0.1.x 维护规则

只接受：

- DCP 格式/版本兼容修复；
- 目标加载、source 不变、commit barrier 的正确性修复；
- 安装、wheel、chart、日志和故障诊断修复；
- 安全问题和小型可用性改进。

不把下面内容塞进 0.1.x：

- 在线 rank 迁移（Live Rank Split）；
- 新 checkpoint 格式；
- 新调度器或训练框架；
- 大规模重写数据面。

0.1 是已发布的历史 release；当前产品验证以本路线图和 `README` 的 release
状态为准。

## 0.1.2：Rescue Demo（第一次体验版）

目标：让一个外部用户在 5 分钟内看懂并在本地复现“checkpoint 被救出来”，
而不是先读一大段文档。

必须交付：

- 两套 kind、无 GPU、无外部 checkpoint；
- 一条命令启动 16→8 本地盘迁移；
- 40% 注入 target agent 失败、65% 注入 controller 重启；
- 显示 journal/execution snapshot 恢复、已提交字节、重传字节；
- target DCP load 8/8 成功；
- 输出 `timeline.html`、`migration-proof.json` 和 source SHA256；
- 同一套场景进入 CI；
- README 首屏有 GIF/短录屏和一条可复制命令。

验收：

- 干净 Linux + Docker/kind 环境，5–10 分钟内完成；
- 不需要 GPU、S3、共享盘或手工 rankMap；
- 故障注入后不误报成功；
- 观众能从 timeline 直接看到“暂停/失败/恢复/完成”。

本地中断/续跑版本已落为 `docs/RESCUE-DEMO.zh-CN.md`；kind 故障矩阵
（target agent 失败、controller restart）仍待执行。

这不是 0.2，也不改变静态 checkpoint 迁移的边界。它只是让 0.1 的价值可见。

## 0.1.3：迁移性能专项

目标：把 768 MiB、4→3、冷缓存场景的 migration 从 `10.43s` 压到接近
`cp -a + DCP load` 的复制基线，同时保持 resume/verify 能力。

必须优化：

1. **single-write DCP export**：去掉 raw target workdir 再封装最终
   `.distcp` 的第二遍写入；
2. **checksum 融合**：避免 inventory、executor 对 source 的重复整读；
3. **fsync/journal 批量化**：每 target rank 一次，而不是每个 plan node 一次；
4. **target rank 并行**：至少并行不同 rank 的独立搬运；
5. 重新跑 768 MiB 冷缓存和 `4→3` 基准。

验收：

- 同机 cold-cache migration wall 不超过 `cp -a + DCP load` 基线的 `1.1x`；
- target load 不慢于当前 DCP 基线；
- source 不变、bitwise、resume、proof 全部回归；
- 继续如实报告：共享盘原生 DCP 仍可能更快，我们只消除实现性开销。

0.1.3 完成后，直接进入 0.2.0。不要再把 0.1.x 扩成新品类。

## 0.2.0：Checkpoint Mobility Control Plane

启动条件：`0.2.0-rc.1` 产品面通过；`0.1.2`/`0.1.3` 是候选后的体验与
性能工作，不阻塞本候选。

### 用户任务

> 当训练任务被抢占或需要换集群时，平台知道它最近一个完整 checkpoint，
> 能把本地分片搬到目标 DeviceMesh，并确认 target Job 真的可以继续训练。

0.2 的三个功能：

1. **Checkpoint-aware preemption / restore**：checkpoint freshness/state
   进入 Kueue 抢占决策，区分 restore 与 fresh start；
2. **Separate-Mesh local-shard migration**：不共享目录，跨 DeviceMesh
   搬运和重分片；
3. **Target-ready integration**：target Job 加载 DCP 并运行 N 个训练 step，
   失败不能误报成功。

三个功能共享 `CheckpointState + MobilityPlan + Transaction + Proof` 内核。

2026-10-22 范围调整：Kueue preemption 是独立工作流，由对应会话继续；
`0.2.0-rc.1` 产品候选只收口 Mobility、target-ready 与诊断体验。LRS 仍为
实验模式，不作为 0.2 发布门槛。

当前分支状态：Feature 2 的本地 DCP 跨 mesh 数据面、metadata-owner 分发和
CPU/CLI 回归已完成；真实 4×L20 与 128 节点 selector 验收仍待执行。
Feature 1/3 不在这个分支内。

### 0.2 必须交付

- Kueue `PreemptionConfig` 可消费 checkpoint freshness/cost；
- source/target 不同 DeviceMesh 的本地分片迁移；
- target Job/TrainJob readiness 与 proof；
- 4×L20 上真实 `4→3` 或 `4→2`；
- 128→64 CPU/控制面规模验证；
- source digest、target load、failure/rollback 日志。

### 0.2 明确不做

- CUDA context / Python 堆的内存在线迁移；
- 任意 tensor/pipeline parallel 布局自动转换；
- 未保存 dataloader/RNG/scheduler 时的“完全相同训练轨迹”承诺；
- Live Rank Split 的 GPU/跨机生产化（放到 0.3+，不是 0.2 发布门槛）；
- 生产 SLA 和任意公网条件下的 RTO 承诺。

详细门槛属于内部发布验收，不放在公开文档树。

## 0.2.x：实现中发现的补丁待办

0.2 开发过程中发现的问题不另开 0.3、0.4 或新大版本，统一进入
`0.2.1`、`0.2.2`、`0.2.3`……补丁待办。只有改变用户任务形态，才重新讨论
大版本。

已知可能进入待办的方向：

- 更多 GPU 拓扑和真实多节点；
- 网络整形、带宽退避和 backpressure；
- dataloader/RNG/scheduler 的可选状态扩展；
- 更丰富的 proof、timeline 和故障注入；
- 训练平台/Kueue 的显式恢复动作集成。

## 0.3：Provider planner + Live Rank Split + Autopilot

从“用户显式迁移”提升到“平台策略触发迁移”：

- 抽象 StateProvider / TransportProvider / TargetLoader；
- 将 Live Rank Split 作为显式可选模式，而不是默认路径；
- 在 DCP 原生、ByteCheckpoint、ETC/Tenplex、TierCheck/FastCheck、
  shardferry range transport 之间做成本/兼容性选择；
- 有共享盘时明确推荐 DCP 原生，不强行走 shardferry；
- spot/节点故障/集群升级事件；
- target capacity 和兼容性预检；
- 迁移策略、配额、审批和冷却；
- 自动 target job 启动与验证；
- 失败后的人工接管路径。

自动发现和自动触发必须建立在 0.2 的 checkpoint state、跨 mesh 迁移和
target-ready 已经可靠之上。

## 1.0：跨控制域状态移动层

长期品类不是单一 DCP 拷贝工具，而是：

> A 有、B 缺时，判断能否迁移，选择安全路径，执行迁移，并证明 B 确实可用。

按 provider 扩展：

1. DCP state provider（0.1 已完成第一版）；
2. 文件、volume、dataset、model artifact；
3. workload manifests、镜像、配置和依赖；
4. 迁移判定、闭包、成本模型和审计 proof；
5. 云上云下、灾备、计划性扩缩容和在线切换。

底层实现优先复用 DCP、rsync、对象存储、CSI、VolumeSnapshot 和厂商 API；
只有它们无法表达迁移语义时才自研。上层控制面和 proof 标准必须自研。

## 当前优先级

1. `0.2.0-rc.1` 产品闭环：版本/能力/CLI/文档一致性，static + cross-mesh +
   target-ready 的真实用户路径；
2. `0.2.0` GPU/cross-machine 门槛；
3. `0.1.2` Rescue Demo；
4. `0.1.3` 性能专项；
5. 实现中冒出的问题按补丁版本登记。

不在当前优先级：更多格式、更多 provider、模型推理和通用工作负载迁移。
