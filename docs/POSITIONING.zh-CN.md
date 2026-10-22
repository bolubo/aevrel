# shardferry 定位

日期：2026-10-22
阶段：0.2.0-rc.1 candidate / Alpha

## 一句话

**shardferry 的长期品类是训练状态的 Checkpoint Readiness & Mobility
Control Plane：持续判断 checkpoint 是否完整、新鲜、可加载、可迁移，
在事件发生时决定 restore、restart、migrate 还是 hold，并留下 proof。**

当前版本先从 local-disk DCP 做起，但迁移只是其中一个执行动作。真正的
产品中心是 CheckpointRecord/Readiness、restore drill、rescue decision 和
可验证的 MobilityTransaction。DCP 是第一条 provider，不是品类上限。

长期不是重写 DCP、ByteCheckpoint、NVRx、Velero、CSI 或云迁移服务，而是
把它们纳入统一的 checkpoint/state control plane，由 planner 做判定、决策、
事务和证明。

我们的默认位置是上层控制面、状态语义和事务证明，不是通用数据面性能竞赛。
底层 provider 能满足语义、正确性和 SLO 时优先集成；只有它无法表达关键
语义、无法恢复或没有稳定 API 时，才考虑自研。

整体问题定义和迁移方案必须自研；内部实现优先复用成熟组件，并通过
provider/adapter 隔离，避免第三方类型和生命周期渗透到核心状态机。

“集群”是用户最直观的说法；更准确的边界是不同控制域。A/B 可以是两个
Kubernetes、云上云下、本地多集群，或单机到集群。同集群内交给
Kubernetes/CSI；A/B 都有且持续同步的主从/双活不是迁移。

## 品类判据

一条迁移成立，至少满足：

1. A/B 属于不同控制域；
2. A 有所需的工作负载或状态，B 缺；
3. 目标环境具备承载能力，或能明确报告不可迁的阻塞项。

B 缺的可以是服务、状态，或两者都缺。只缺服务时，迁移对象是 manifests、
镜像、配置、依赖和接线；只缺状态时，迁移对象是数据/checkpoint；两者都缺
时按依赖顺序一起迁。通用迁移不承诺重写应用本身。

## 解决的问题

典型训练任务：

- 128 个 rank 运行在多台机器上；
- 每个 rank 把 DCP shard 写在本机 NVMe；
- 任务退出、节点故障、集群更换，或目标环境只有 64 张卡；
- 没有足够的共享存储，也不希望先把整个 checkpoint 汇总到 S3。

用户需要的是：

1. source checkpoint 不被修改；
2. 不产生全局全量副本；
3. 能改变 world size 和 DeviceMesh；
4. 传输失败后可以续跑；
5. target 数据经过 bitwise 验证后才提交；
6. 最终得到目标训练任务可以加载的 DCP。

## 用户

第一用户是 AI Infra / training platform engineer，不是模型算法工程师。

他们负责：

- 训练集群和 checkpoint 生命周期；
- 节点、存储、Kubernetes 和 GPU 资源；
- 故障恢复和集群迁移；
- 迁移后的可验证性。

第二用户是需要在不同 GPU 数量之间恢复训练的研究团队。

## 产品形态

### 单机 / 共享文件系统

```bash
shardferry demo
shardferry doctor --checkpoint /path/source.dcp
shardferry migrate --checkpoint /path/source.dcp --target-world 64
```

适合所有 shard 已经在一台机器或共享路径可见的场景。

### Kubernetes local disk

```bash
shardferryctl preflight -f migration.yaml
shardferryctl migrate -f migration.yaml --wait
```

source/target agent 按节点的 hostPath 工作，controller 通过 label selector
发现 agent 和本机 ranks。用户不写 rankMap。

### 未来形态

在线 cutover、CDP 持续追赶和 RPO/RTO 是独立能力，只有在真实 target load
和故障矩阵验证后才进入稳定产品面。

在 DCP 垂直场景稳定后，产品按 provider 扩展：state provider（文件、
volume、dataset）、workload provider（manifests、镜像、配置、依赖）和
迁移判定/规划 provider。控制面可以是 Kubernetes-native，但内核状态机不能
绑死 DCP 或 AI。

## 非目标

- 通用 checkpoint 格式转换；
- 训练调度器或 Kueue/Kubernetes 替代品；
- 模型推理和 serving；
- 通用分布式文件系统；
- 对所有 checkpoint 格式、所有存储后端和所有网络环境的一次性支持；
- 同集群内 Pod/卷重调度（交给 Kubernetes/CSI/平台现有能力）；
- A/B 双活、持续主从复制和 CDC；
- 承诺所有工作负载都可迁；不能迁时必须报告阻塞，不重写应用；
- 当前 0.2 不交付通用 workload provider。

## 核心差异

不是“再写一个 checkpoint 拷贝工具”，而是：

- **DCP-aware**：理解 tensor/chunk 和 DeviceMesh；
- **local-disk-first**：默认不要求全局远程 checkpoint；
- **topology-aware**：plan 直接按 target rank 和 mesh 划分；
- **verifiable**：bitwise 校验和 commit barrier 是产品语义；
- **resumable**：journal、staging 和幂等 task；
- **operator-friendly**：Kubernetes selector 发现，不要求手写 128 条映射。

## 当前边界

- 本地 DCP 路径：可用，作为 Alpha 重点；
- Kubernetes local-disk selector：代码闭环已具备，真实多节点验收待补；
- GPU/FSDP 真实加载：待环境验证；
- 在线 cutover：实验能力，不作为当前稳定承诺。

## 判断标准

后续功能只有满足以下至少一项才进入主线：

1. 降低真实用户完成一次迁移的操作复杂度；
2. 提高 source 安全、bitwise 正确性或故障恢复能力；
3. 让 local-disk / 多节点 / GPU 的真实闭环更容易验证；
4. 让安装、诊断或目标加载更可靠。

否则进入 backlog，不因为“技术上很酷”而进入产品面。

品类和市场分析属于内部产品研究，不放在公开文档树。
