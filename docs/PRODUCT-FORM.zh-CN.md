# Aevrel 产品形态与日常价值

状态：Aevrel 0.3.0 当前产品方向

## 0. 一句话

Aevrel 是 **scheduler-neutral 的 Training State Control Plane**：
把 checkpoint 和训练运行时状态变成可查询、可判断、可执行、可证明的事实。

Checkpoint 是第一个完整 vertical；迁移和 LRS 只是控制面按需执行的动作；
Kueue、Volcano、Kubeflow 和未来 UI 都是这些事实的消费者。

## 1. 日常价值

平台每天要回答：

1. 最新完整 checkpoint 在哪？
2. 所有 rank/shard 是否齐全、版本是否一致？
3. 距离现在多久，最多丢多少进度？
4. 最近一次真实 target load / restore drill 是否成功？
5. 目标集群和拓扑是否兼容？
6. 抢占或故障后应该 restore、restart、migrate 还是 hold？

这四项状态是产品契约：

| 信号 | 含义 |
|---|---|
| `complete` | 所有 rank 的 shard 齐全，metadata 一致 |
| `fresh` | 最新完整 step、时间、最大丢失量 |
| `loadable` | 最近一次真实 target load / drill 的结果 |
| `movable` | source/target world size、mesh、DCP 版本、状态闭包兼容 |

派生状态：

```text
READY | STALE | INCOMPLETE | TARGET_BLOCKED | UNVERIFIED
```

对应动作：

```text
readiness      查询一个 job 或整个集群
restore-drill  显式触发真实 target load + N step
rescue-decide  restore / restart / migrate / hold
migrate        static 或 live CheckpointMigration
proof          source digest、target readiness、timeline
```

`restore-drill` 是显式触发的验证动作，不是系统自行创建的定时任务。

## 2. 状态对象

```text
CheckpointRecord
  jobUID, namespace, version, step
  ranks, shard locations, metadata digest
  complete, freshness, lastVerifiedAt

CheckpointReadiness
  complete, fresh, loadable, movable
  status, reasons, estimatedRTO

RestoreDrill
  version, target topology, steps
  modelLoaded, optimizerLoaded, stateComplete
  loss/parameter comparison, proofRef

RescueDecision
  event, decision, reason, target, proofRef

MobilityPlan / MobilityProof
  source -> target, bytes, topology, transaction
  source digest, target digest, coverage, quorum
```

## 3. 同类项目与替代方案

| 方案 | 覆盖 | 不覆盖 |
|---|---|---|
| Kueue `#477` | 抢占候选、队列/配额 | checkpoint 状态、inventory、target readiness |
| Volcano `#5998` | 抢占 victim ordering 设计 | 通用 checkpoint state；issue 以离线讨论关闭 |
| Kubeflow Trainer `#2245`/`#2777` | TrainJob、exporter、CRIU | local-disk 跨控制域、DCP 拓扑兼容、统一 proof |
| TorchFT `#329`/`#343` | quorum/fault tolerance | 日常 checkpoint 健康度、目标 readiness、跨控制域 |
| TorchTitan CheckpointManager | interval、retention、async、HF 转换 | 训练栈外 fleet 状态、target readiness |
| NVRx | local/async checkpoint、fault tolerance、health | 中立控制面、跨控制域、统一 proof |
| PyTorch DCP / ByteCheckpoint | save/load/reshard/单 checkpoint 管理 | fleet 状态、source/target 事务、proof、rescue |
| Orbax | JAX async/retention/manager | PyTorch DCP、local-disk、跨控制域 |
| Determined AI / Ray Train | 平台内 checkpoint 生命周期 | 中立、local-disk、跨控制域；绑定各自平台 |
| Velero / CSI / 对象存储 | 备份、卷、字节复制 | DCP rank 语义、状态闭包、target 可训练性 |

调研信号：

- Kueue `#477` 从 2022 保持到 2026，明确说调度器不知道
  “是否 checkpoint、多久之前、restore 还是 restart”。
- Volcano `#5998` 提出 checkpoint-aware victim ordering，但方案只读
  Pod annotation，没有通用状态层。
- Kubeflow Trainer `#2245` 仍在讨论 exporter/checkpoint lifecycle；
  `#2777` 走 CRIU，不是跨拓扑状态迁移。
- TorchFT `#329` 有用户报告故障恢复接近 15 分钟；恢复时间本身是运维痛点。
- PyTorch DCP 的 load-time resharding 依赖可访问的 checkpoint storage，
  不负责 local-disk 跨控制域、target readiness 或 fleet inventory。

结论：需求信号成立，但现有方案碎片化；没有发现一个中立的、跨控制域的
checkpoint readiness 层。这个判断以内部验收矩阵和真实环境测试为准。

更完整的生态盘点、竞品矩阵和用户证据属于内部产品研究，不放在公开文档树。
模块边界、接口和依赖方向见
[控制面架构](ARCHITECTURE.zh-CN.md)。

## 4. 我们的定位

我们做：

- 状态事实层：CheckpointRecord、Readiness、inventory、freshness；
- 验证层：真实 target load、restore drill、状态闭包、proof；
- 决策层：rescue decision、mobility plan、target compatibility；
- 事务层：source read-only、fence/commit/rollback、跨控制域移动；
- 执行层：static mobility、target-ready、按需 LRS worker。

我们不做：

- 不重写 Kueue/Volcano/Kubeflow 调度器；
- 不重写对象存储、CSI、Velero、DCP save/load；
- 不承诺任意进程/GPU 内存热迁移；
- 不要求所有用户常驻 agent；
- 不用“文件存在”冒充“target 可训练”；
- 不把 Kueue 当作唯一入口或唯一留存理由。

差异化：

- neutral：不绑定单一调度器、存储或训练框架；
- local-disk-first：理解 rank-local shard；
- cross-control-domain：A/B 独立、不共享存储；
- decision-grade：输出状态和动作，不只是日志；
- provable：source digest、target load、coverage、timeline；
- transactional：失败不会留下“看起来成功”的半状态。

## 5. 产品形态

| 形态 | 组件 | 生命周期 | 用户 |
|---|---|---|---|
| 单次工具 | `aevrel` CLI / one-shot Job | 用完即走 | 偶尔迁移 |
| 平台控制面 | `aevrel-controller` + `aevrel-agent` | 常驻 | AI 平台/运维 |
| 调度集成 | Kueue/Volcano/Kubeflow adapter | 随平台常驻 | 平台团队 |
| 低停机迁移 | `aevrel-lrs-worker` | 迁移时按需 | 高阶训练平台 |

推荐部署：

```text
管理集群
  aevrel-controller
  CheckpointRecord / CheckpointReadiness
  proof/audit store

训练集群
  aevrel-agent (DaemonSet, read-only)
  可选 gateway / LRS worker

用户/平台
  aevrel CLI
  aevrelctl
  scheduler adapter (optional)
```

留存逻辑：

- 只需要一次迁移的用户用 CLI/Job，允许不常驻；这是正常获取漏斗。
- 需要持续可靠性的平台常驻 controller/agent，因为每天要回答
  “现在能不能恢复/迁移”。
- Kueue/Volcano/Kubeflow 是消费者，不是产品本体；没有它们 readiness
  仍然独立工作。
- LRS 是 Mobility 的执行模式，不是独立常驻产品。

## 6. 执行步骤

### P0：定义冻结

交付：本文件、schema、错误码、状态机、用户手册骨架。

验收：每个状态唯一；不允许“文件存在即成功”；Kueue/Volcano/Kubeflow
只作为 adapter，不进入核心状态机。

### P1：只读 Inventory + Readiness

交付：agent 只读扫描 local roots；controller 聚合 rank/shard；readiness
CLI/API；freshness/coverage/version/source digest。

验收：缺失/重复/半写/不可读/版本不一致全部识别；source 不变；1000 个
逻辑 checkpoint 的 readiness 查询 p95 <= 2 秒；状态变化 30 秒内可见。

### P2：Restore Drill

交付：显式 `restore-drill`；真实目标进程加载 model + optimizer；至少 N 个
训练 step；更新 `loadable` 并产出 proof。

验收：不允许只查 `.metadata` 或文件大小；必须证明 model/optimizer 加载、
状态闭包和 N step；失败不能标成 `READY`；`stateComplete=false` 必须显示。

### P3：Rescue Decision

交付：K8s eviction/failure/restart 事件源；`rescue-decide`；决策依据；
Kueue/Volcano adapter 可选。

验收：事件后 5 秒内给决策；有 reason/proof；不可恢复明确 hold/unrecoverable；
Kueue 未安装不阻塞基础 readiness。

### P4：Static Mobility

交付：static DCP migration；cross-mesh/local-disk；target-ready 成功门禁；
journal resume、atomic commit、source read-only。

验收：source digest 不变；target 真实加载并跑 N step；任何 rank 失败不能
报告整体成功；失败可续跑；不产生半状态 target。

### P5：LRS / Live

交付：LRS controller 契约；按需 worker；用户可配置 `maxInFlight`；
coverage/quorum/state closure；source-death takeover。

验收：4 GPU 单节点 NCCL reconfigure；2 节点 A/B 无共享存储；
成功场景 `steps_lost=0`、`global_stop=0`；active rank 不用 async/双模型；
失败 fail closed，不静默回退 static。

### P6：规模、故障与发布

交付：128 rank / 10k checkpoint；agent/controller 重启、source 死、target
失败、网络分区、磁盘满、checksum 错、stale shadow；RBAC/mTLS/审计；
release/upgrade/rollback/support bundle。

验收：所有故障要么完整 proof 成功，要么 fail closed；不允许静默成功；
关键指标/timeline/proof 可复核；内部 acceptance matrix 全通过，不依赖外部访谈。

## 7. 总体验收

### A. 日常价值

- 5 分钟内回答任意训练的最近可恢复状态；
- `complete/fresh/loadable/movable` 与 ground truth 100% 一致；
- `loadable` 必须来自真实 drill，不接受 metadata-only；
- readiness 不读全量 checkpoint 数据。

### B. 正确性

- source 不变；
- target 有 proof；
- coverage 覆盖全部 rank；
- commit/fence/epoch 无双 owner；
- stale、重复、越界、半写均 fail closed。

### C. 故障

- agent 重启后恢复 inventory；
- controller 重启后从 journal/CRD 恢复；
- source 死亡只按 quorum/coverage 接管；
- target 写失败/checksum 错/磁盘满不 commit；
- 网络分区不产生 split-brain。

### D. 规模与性能

- 128 ranks、10k checkpoints；
- readiness p95 <= 2 秒（缓存路径）；
- 状态变化 <= 30 秒可见；
- restore drill/migration 的耗时、RSS、字节数有记录。

### E. 用户体验

- `aevrelctl readiness` 一条命令给出状态、原因、下一步；
- 失败输出包含 `Next:`；
- `--json` schema 稳定；
- static 显示字节进度，LRS 显示 rank/ownership 进度；
- live 不可用时明确失败，不自动 fallback。

### F. 安全与运维

- source 只读；
- 最小 RBAC；
- token/mTLS 可轮换；
- proof/support bundle 不含 Secret；
- 兼容/升级/回滚可执行。

## 8. 不可降级红线

1. 不用文件存在代替 target load；
2. 不用 metadata 检查代替 restore drill；
3. 不用 static 全量停机偷偷替代 live/LRS；
4. 不用单机共享盘冒充跨控制域；
5. 不用 CPU 模拟冒充 4-GPU/2 节点；
6. 不用 Kueue 单点集成冒充产品本体；
7. 不用“会迁移”代替日常 readiness；
8. 不用未验证 future work 填补当前验收。

## 9. 当前状态与下一步

已完成：static DCP 数据面；控制面、target-ready 和局部 cross-mesh；
`CheckpointRecord` 多 agent selector/128-rank 合并；
`RestoreDrill` 自动 Indexed Job 与 version+digest 绑定 proof；
`RescueDecision` 通用 K8s Warning event 入口；LRS CPU4 前置和
torchcomms probe；本地 rescue demo、进度 UX 和 LRS 用户流程。

下一步：

1. 本地 acceptance matrix 和 kind 多 Pod/128-rank 环境验证；
2. 需要新事务抽象时，再设计直接驱动 planning/executor 的资源；
3. 把 decision 接到显式执行入口，但保持不自动启动第二份训练；
4. 做 LRS 按需 worker 接入与 4 GPU/2 节点验收。

Kueue 集成继续由独立工作流负责；它只能作为 adapter，不是本产品成立的
前置条件。
