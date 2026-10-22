# shardferry 竞争与移动层策略

状态：内部策略草案

## 1. 判断

需求真实，但“有人需要移动 checkpoint”不等于“复制 checkpoint 本身能成为
长期护城河”。竞争不是某一个 shardferry killer，而是平台化：

- PyTorch DCP 吸收共享存储场景的 load-time reshard；
- ByteCheckpoint 做统一保存/加载和重分片；
- ETC/Tenplex 尝试用 P2P 或运行时并行切换绕过 checkpoint；
- FastCheck/TierCheck 降低本地保存和故障恢复延迟；
- CSI、VolumeSnapshot、对象存储和 Kueue/Kubeflow 吸收底层搬运与触发。

如果 shardferry 只比同机复制速度，会被持续挤压。

## 2. 守什么

守**跨控制域的移动事务**，不守 checkpoint 的保存/加载速度：

1. source/target 不共享控制面、文件系统或对象存储；
2. 拓扑不同，需要判断哪些 range 可以复用、哪些必须搬；
3. 中断后按 chunk/journal 恢复；
4. commit 前验证，commit 后不允许 source/target 同时运行；
5. 失败可 rollback，半成功不得报告成功；
6. target readiness 和迁移证明可独立验证；
7. 可被 Kueue/Kubeflow/平台调用，而不是只靠 CLI。

一句话：

> DCP 负责“状态如何保存/加载”，shardferry 负责“状态如何跨控制域安全到达、
> 接管并证明可用”。

## 3. 吸收矩阵

| 方案 | 吸收方式 | 不重写 |
|---|---|---|
| PyTorch DCP | source/target state provider、target load、同 world 重分片 | tensor/DTensor 重分片数学和 DCP 文件格式 |
| ByteCheckpoint | checkpoint source/target adapter 或格式边界 | 保存/加载引擎和统一 checkpoint 管理 |
| ETC / Tenplex | 可用时的 P2P/零物化 fast path | GPU 间直接状态传输和动态并行 |
| FastCheck / TierCheck | 本地 checkpoint、分层存储和本地故障恢复 provider | 单集群恢复延迟和故障分类 |
| nvRX / 本地快存 | source checkpoint 的高速本地保存层 | 驱动、文件系统和厂商存储 |
| CSI / VolumeSnapshot / 对象存储 | transport/copy substrate | 块存储、快照和对象存储服务 |
| Kueue / Kubeflow / Kubernetes | 触发、放置和策略层 | 通用调度器 |

策略不是“全都支持”，而是：planner 根据场景选择 provider，shardferry
拥有跨域事务和证明。

### DCP 的特别规则

有共享文件系统且 DCP 原生 load 更便宜时，preflight 应明确推荐原生路径。
不要为了多搬一次而强行使用 shardferry。信任比“所有流量都走我们”更重要。

### ETC / Tenplex 的特别规则

source/target 网络连通、任务仍活着且 P2P 可行时，planner 可以选择它们；
网络隔离、source 已死、跨管理域或需要 checkpoint proof 时，回退到
shardferry 的 checkpoint mobility。

### TierCheck / FastCheck 的特别规则

本地故障优先用本地快速恢复；跨集群容量调整、集群替换和跨云灾备再进入
shardferry。两者串联，不是二选一。

## 4. 平台化的四个接口

平台不是 Web UI，而是别人可以依赖的契约：

1. **StateDescriptor**：job、version、step、tensor layout、provider handle；
2. **MobilityPlan**：哪些 range 必须搬、目标放在哪里、成本/带宽/风险；
3. **MobilityTransaction**：fence、pause、transfer、verify、commit、rollback；
4. **MobilityProof**：source digest、target proof、timeline、target readiness。

Provider 只回答“如何读/写/验证”，不拥有跨域事务语义。

## 5. 发展顺序

- `0.1`：DCP 静态移动；
- `0.1.2`：Rescue Demo，首次体验；
- `0.1.3`：性能专项，消除可优化 I/O；
- `0.2`：Checkpoint Mobility Control Plane，checkpoint-aware preemption、
  跨 mesh 移动和 target-ready；
- `0.3`：Provider planner + Autopilot，开始吸收 DCP/ByteCheckpoint/
  ETC/Tenplex/TierCheck；
- `1.0`：跨控制域状态移动层，成为调度器和平台可调用的 primitive。

平台不能自我宣告。至少出现以下两项才算开始成为平台：

- 外部 provider 按我们的接口接入；
- 外部调度器/训练平台调用我们的 handoff；
- 外部团队用我们的 proof/contract 做验收；
- 有第二个 state provider，而不是只有 DCP。

## 6. 不做什么

- 不重写 DCP 的 tensor 重分片；
- 不做比 ByteCheckpoint/FastCheck 更快的保存引擎；
- 不重写 ETC/Tenplex 的 P2P 状态迁移；
- 不做 TierCheck 的本地分层恢复；
- 不做通用对象存储、CSI、快照或调度器；
- 没有第二个 provider 需求前不堆插件动物园；
- 不把同机共享盘“原生更快”包装成失败。

## 7. 竞争壁垒

可被复制：CLI、HTTP range、DCP adapter、基础 planner。

不容易被复制：

- 跨域 fence/commit/rollback 的正确性；
- source 不变、target proof、失败重传语义；
- 多 provider 的成本/兼容性/失败分类；
- 与平台的默认集成和运维可信度；
- 公开、可复现的 RTO/RPO 与 90% 故障数据；
- 迁移协议被外部 provider 采用后的网络效应。

## 8. 唯一值得追的 benchmark

不是同机 MB/s，而是跨域 checkpoint-aware preemption / migration：

- `time_to_first_step`
- `steps_lost`
- `cutover_pause`
- `cutover_delta_bytes`
- 90% failure 后的 `retransferred_bytes`
- target 继续训练的 step 数

如果这些数字不能形成明显优势，就缩小定位，而不是继续堆低层功能。如果它们
成立，移动层才有机会成为平台默认路径。
