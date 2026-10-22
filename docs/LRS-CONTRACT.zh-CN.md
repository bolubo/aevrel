# LRS Contract v1（Live Rank Split）

状态：M0 冻结候选（本地草案，未推送）
日期：2026-10-22
适用范围：shardferry 跨集群、无共享存储、同步训练状态迁移

## 0. 一句话

在 A/B 两个独立集群之间，保持逻辑 world size `W` 不变，允许 `F` 个 rank
同时在途；剩余 `W - F` 个 rank 继续同一个同步 global step，每个 shard
始终有唯一 owner，任何时刻不出现双训练、async 或整任务停机。

## 1. 术语

- `W`：逻辑 world size（迁移前）。
- `F`：同时在途的 rank 数，`1 <= F < W`。
- `active`：`W - F`，仍在参与同步 step 的 rank 数。
- `stable step`：所有 active rank 完成同一个 optimizer step，且 ownership
  map 一致。
- `shard`：模型/优化器状态的最小可迁移单元（DCP shard、ZeRO shard、
  TP slice、PP stage 参数片）。
- `owner`：对 shard 有写权限的 rank。
- `epoch`：ownership 的单调递增版本号。
- `lease`：rank 的写租约；过期即被 fence。
- `coverage`：所有 shard 恰好有一个 primary owner；shadow 不算 primary。
- `commit quorum`：正常 ownership 切换所需的最小当前成员数，默认当前
  membership 的多数。
- `takeover quorum`：source 死亡后组成 degraded world 所需的最小成员数，
  默认原始 `W` 的多数；不得低于 2。

## 2. 不变量（不可谈判）

I1. **单一 owner**：任何 stable step，每个 shard 恰好一个 primary owner；
    epoch 单调递增；旧 epoch 的写必须被拒绝。

I2. **同步 step 连续**：迁移期间 global step 不跳步、不重复、不退化成
    async/LocalSGD/双模型；active rank 继续同一个 optimizer step。
    注：禁止的是 async **训练语义**，不是异步 I/O；snapshot/delta 可以
    流水线传输，但 target 只能在 stable step 的一致点成为 primary。

I3. **全局 batch 不变**：在途 rank 的数据分片由 active rank 吸收
    （work redistribution）或由 shadow 覆盖；不得通过缩小 global batch
    来“继续”。

I4. **coverage 每步校验**：stable step 边界必须证明
    `union(owner shards) == all shards`；不成立则不得推进 step，迁移回滚
    或 fail closed。

I5. **quorum**：ownership 切换和 degraded world 成立必须获得对应 quorum；
    quorum 不成立时不得单方面 commit。

I6. **fence 先行**：source 侧在 cutover 前必须进入 read-only 并被 fence；
    target 只有在 fence 确认后才能成为 primary。

I7. **A 死亡**：仅当 B 拥有 takeover quorum + 完整 coverage 时，才能组成
    degraded world；否则 fail closed，不得报告成功、不得留下半状态。

I8. **无 checkpoint-restart 回退**：LRS 路径不得在内部退化为“停任务→写
    checkpoint→重启”；全量停机模式是独立模式，不是 LRS 的 fallback。

I9. **可证明**：每次迁移产出 proof：source digest、target digest、
    step range、epoch、coverage 集合、quorum 成员、时间线。

I10. **不双训练**：cutover 后 source 的该 shard 永久只读；source 不得在
     target 接管后继续更新同一 shard。

## 3. 明确不做（v1 红线）

- 不做跨集群异步参数迁移（ElasWave 路线）。注：异步传输允许；target 在
  stable step 前只能是 shadow，不能参与 optimizer step。
- 不做整任务停机切换（DynaTrain 的 masked interruption 路线）。
- 不做 checkpoint-free P2P 替代存储（ETC 路线）；LRS 允许在 stable step
  生成一致性快照，但训练不停止。
- 不做无 quorum 的 split-brain 自动继续。
- 不做“target 只 warm 不 join”的假迁移。
- 不把 `F=1` 的单 rank 迁移宣传成“多 rank 同时迁移”。
- 不把同机/同集群结果宣传成跨集群结果。
- 不支持 topology 时 preflight 必须拒绝，不得静默降级。

### 3.1 拓扑矩阵：什么时候不该用 shardferry

共享高速网络本身不会让 shardferry 失去意义。让 shardferry 失去意义的是
**共享控制面 + 共享存储 + 共享故障域同时成立**——那时问题退化成单集群
重分片，应该直接用 DCP/K8s 原生能力。

| 拓扑 | 共享控制面 | 共享存储 | 共享故障域 | 推荐 |
|---|---|---|---|---|
| 同集群、共享 FS | 是 | 是 | 是 | DCP 原生；shardferry 不介入 |
| 同集群、无共享 FS | 是 | 否 | 是 | shardferry 搬运；LRS 可选 |
| 同 DC、不同集群、高速网、共享 FS | 否 | 是 | 部分 | 优先 DCP/ByteCheckpoint；需要跨域事务时用 shardferry |
| 同 DC、不同集群、高速网、无共享 FS | 否 | 否 | 部分 | **shardferry 核心场景**；LRS 可行 |
| 跨区域/跨云、无共享 FS | 否 | 否 | 否 | **shardferry 核心场景**；LRS 的 delta/续传/证明最关键 |

判断标准不是“是不是 WAN/VLAN”，而是：

1. 控制面是否独立（K8s/调度器/账号/配额）；
2. 存储是否可达（共享 FS/对象存储 vs. 完全隔离）；
3. 故障域是否独立（同机房/同电源/同交换机 vs. 独立）；
4. 是否需要跨域事务证明（fence/commit/rollback/proof）。

只要 1、2、3 中有一项是“独立”，就存在跨域问题；网络快慢只改变传输
成本，不改变语义难度。三项全部共享时，preflight 应明确推荐原生路径，
不得为了使用率强行接管。

## 4. 与 prior art 的边界

| 工作 | 它覆盖 | 它不覆盖 | LRS 的差异 |
|---|---|---|---|
| ElasWave（arXiv 2510.00606） | 单集群 hybrid-parallel online resharding；async 参数迁移；dynamic communicator；RNG resharding | 跨独立集群；无共享存储；同步 step 不碎；F rank 同时在途 | LRS 禁止 async；要求 active 继续同一同步 step；跨控制域 |
| DynaTrain（arXiv 2605.18815） | VPS；任意多维并行切换；70B < 2s；整任务迁移；dynamic scaling 中断窗口 1.91s/3.05s | W 固定、F rank 同时在途、其余 rank 不停同一个 step；跨集群无共享存储 | LRS 的 `steps_lost = 0` + `global_stop = 0`；不是整任务切换 |
| ETC（arXiv 2607.04749） | checkpoint-free；GPU P2P；单集群 interconnect；2.33–6.37x | 跨集群；无共享存储；per-rank live split | LRS 的 transport 是跨域；stable-step 快照用于 proof/恢复 |
| TorchElastic | DP 级成员变更；`sync()` | 细粒度 shard/TP/PP 重分片；global step 屏障 | LRS 是 shard 级 ownership 切换 |
| gVisor/nvcf CRIU PR | 进程级 GPU/NCCL checkpoint/restore | 训练语义连续性；同步 step；跨集群 | LRS 在训练语义层，不在进程层 |

结论：**“在线重分片/弹性训练”不是无人区；LRS 的未覆盖组合是跨独立集群
+ 无共享存储 + W 固定 + F rank 同时在途 + active 继续同一同步 step +
quorum/coverage + source-death degraded takeover。**

## 5. 攻关步骤

### P0 语义与协议（M0）

- 写状态机：rank 状态（active/draining/in-flight/arrived/fenced/dead）、
  shard 状态（owned/moving/shadow/committed）、epoch/lease。
- 写 10 条不变量对应的可执行检查。
- 写 fault model：网络分区、source 进程死、target 写失败、checksum 错、
  重复包、stale epoch、时钟偏移、OOM。
- 产出：本文件 + `lrs/spec.py`（或 Go 包）+ 属性测试。
- 验收：模拟器跑 `W=4/8`、`F=1/2`，注入 8 类故障，所有不变量不被破坏；
  发现反例必须修协议，不得改测试。

### P1 stable-step 快照与 delta（M1）

- 在 stable step 边界冻结 shard，生成 snapshot；之后的更新写 delta log。
- delta 必须可按 step range 重放；target 落后不超过 `K` 步。
- 验收：单机 CPU/gloo，`W=2+2`、`F=1`；snapshot + delta 重放后，
  target 与 source 在 step `S+k` 的 shard digest 一致（定点 toy model
  bitwise；fp32 容差 `<= 1e-6`）。

### P2 ownership 切换与 fence（M1）

- lease/epoch/fencing；source read-only；target primary；旧 epoch 写拒绝。
- 验收：注入 stale epoch 写、重复 commit、source 延迟写；全部被拒绝；
  无双 owner。

### P3 work redistribution（M1）

- active rank 吸收 in-flight rank 的 data shards；global batch 不变。
- 验收：baseline（无迁移）与 LRS run 的 step count、global batch、loss
  曲线一致；toy model bitwise，fp32 容差内。

### P4 F>1 并发（M2）

- 多个 rank 同时在途；delta streams 并行；ownership map 并发更新。
- 验收：`W=8`、`F=1/2/3`；每个 stable step coverage 成立；无死锁；
  target 全部 join；step 不跳。

### P5 跨集群传输（M3）

- 两个独立网络命名空间/两台机器；无共享存储；HTTP/QUIC 分块、重试、
  断点续传、checksum。
- 验收：`4+4`、`W=8`、`F=1/2`；网络中断/重连；target 8/8 join；
  source 停止后记录 time-to-first-step；无整任务停机。

### P6 source-death takeover（M4）

- A 在迁移中途被杀；B 用已提交 snapshot + delta 组成 degraded world。
- 验收：coverage 完整 + takeover quorum 成立 → B 继续，step 从最后一个
  stable step 接上；coverage 不完整 → fail closed，退出码非 0，无半状态。
- 两个子场景都要过：可接管 / 不可接管。

### P7 TP/PP 重配置（M5）

- TP group 重建、PP stage 重新映射；communicator rebuild；shadow replica
  或 VPS-like 映射。
- 验收：4 GPU，`TP=2/PP=2`；迁移 1 个 TP rank 和 1 个 PP stage；target
  参与 collective；数值与 baseline 一致（容差内）。

### P8 规模与故障矩阵（M6）

- `8+4`（`W=12`）、`16+8`（`W=24`）；`F=2/4`；CPU/gloo 协议级 +
  4 GPU 硬件级。
- 故障矩阵：网络分区、source 死、target 死、磁盘满、checksum 错、OOM、
  时钟偏移、重复投递、stale epoch、controller 重启。
- 验收：所有场景要么成功且 proof 完整，要么 fail closed；不得静默成功；
  成功场景 `steps_lost = 0`。

## 6. 验收标准（总）

### A. 语义

- A1：baseline vs LRS，toy model bitwise 相同；fp32 模型
  `max|Δw| <= 1e-6`，loss 曲线一致。
- A2：step count 连续；无跳步/重复；global batch 不变。
- A3：每个 stable step coverage 成立；无双 primary owner。
- A4：无 async/LocalSGD/双模型路径（代码审计 + 运行时断言）。

### B. 故障

- B1：stale epoch 写 100% 拒绝。
- B2：source 死：可接管场景 B 继续；不可接管场景 fail closed。
- B3：target 写失败/checksum 错：重试或回滚；不得 commit 半状态。
- B4：网络分区：quorum 不成立时不得单方面继续。
- B5：controller 重启：从 journal 恢复，不重复 commit。

### C. 性能（不作为 v1 门槛，但必须记录）

- C1：`steps_lost = 0`（成功迁移）。
- C2：`global_stop = 0`；允许 per-shard bubble，记录 bubble 步数。
- C3：time-to-first-step on B；迁移期间 per-step slowdown；
  delta bytes / snapshot bytes。
- C4：与全量停机模式对比：停机时间、传输字节、恢复时间。

### D. 证据

- 每次验收产出 `proof.json`：source/target digest、step range、epoch、
  coverage、quorum、timeline、fault inject log。
- 证据目录：`evidence/lrs/<date>-<milestone>/`。
- 无 proof 不得声称通过。

## 7. 当前威胁与窗口

- DynaTrain 是最接近的竞品；它的 dynamic scaling 已经把中断压到
  1.91s/3.05s。如果它在下一版把“整任务切换”改成“per-rank 切换 +
  跨集群”，LRS 的窗口会迅速关闭。
- 因此优先级：先做 M1/M2 的语义证明（CPU 即可），再上 GPU。语义证明是
  护城河；GPU 只是把同一协议跑在 NCCL 上。
- 网络越快，纯传输越不值钱，ownership/fencing/quorum/coverage 越值钱。
  叙事必须是“跨独立故障域的同步训练语义”，不是“跨 WAN 搬字节”。
- 不在公开 README 写“没人做”；写“与 ElasWave/DynaTrain/ETC 的差异”，
  把 prior art 变成我们的坐标系。

