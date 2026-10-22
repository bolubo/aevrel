# shardferry 定位

日期：2026-10-22
阶段：Alpha / preview

## 一句话

**shardferry 是面向 local-disk 训练集群的 DCP checkpoint 迁移层：不先把
checkpoint 汇总到全局存储，而是直接按目标拓扑搬运和 reshard。**

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

## 非目标

- 通用 checkpoint 格式转换；
- 训练调度器或 Kueue/Kubernetes 替代品；
- 模型推理和 serving；
- 通用分布式文件系统；
- 对所有 checkpoint 格式、所有存储后端和所有网络环境的一次性支持。

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
