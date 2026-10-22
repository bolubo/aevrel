# LRS prior art：吸收什么，不重写什么

状态：内部设计备忘（2026-10-22）
目的：把“别人已经做完的”与“我们真正要攻的”分开，避免把商品当爆点。

## 1. 结论

- **communicator 动态重配不是无人区**：NCCL 已有
  `ncclCommGrow` / `ncclCommShrink` / `ncclCommSuspend` /
  `ncclCommResume` / `ncclCommRevoke`；torchcomms 已经实现
  `TorchCommNCCL::reconfigure`（shrink + grow + TCPStore rendezvous）。
- **PyTorch stable 只暴露 shrink，不暴露 grow**：
  `Backend::shrink` / `distributed_c10d.shrink_group` 存在；grow 没有。
  PR #199240（port torchcomms shrink/grow fast path）和 #189359
  （reconfigure fault tolerance）都是 closed、未 merge。
- 所以“target 加入 collective”本身不是我们的爆点；它是应该被吸收的依赖。
- 我们真正要攻的是：**跨独立集群的状态搬运 + 同一个同步 step 的连续性 +
  fence/commit/proof**。communicator 用 torchcomms，state/step/cross-domain
  用 shardferry。

## 2. 已核实的事实

### NCCL

- `ncclCommGrow(comm, nRanks, uniqueId, rank, newcomm, config)`：
  - 老 rank：`comm=旧 comm, uniqueId=新 UID, rank=-1`；
  - 新 rank：`comm=NULL, uniqueId=新 UID, rank=分配 rank`；
  - 成功后拿到包含新旧成员的新 communicator。
- `ncclCommShrink(comm, excludeRanks, count, newcomm, config, flags)`：
  - 移除 rank，剩余 rank 重新排序；
  - `NCCL_SHRINK_ABORT` 会先终止 parent 的 in-flight 操作。
- `ncclCommRevoke`：停止 in-flight，进入 quiescent 后可 destroy/split/shrink。
- `ncclCommSuspend/Resume`：挂起 communicator、释放动态显存。
- torchcomms 的 grow/shrink fast path 要求 **NCCL >= 2.29**。

### torchcomms

- `TorchCommBackend::reconfigure(ReconfigureOptions)`；
- `TorchCommNCCL::reconfigure`：
  - quorum 为空 / identity reconfigure：fresh init；
  - 有 quorum + joiners：`commShrink(NCCL_SHRINK_ABORT)` + `commGrow`；
  - 用 TCPStore/PrefixStore 交换 grow UID；
  - 新 rank 用 `comm=nullptr` grow。
- 它解决的是**单 job 内的成员变更/容错**，不是跨集群 state mobility。
- 它依赖一个可达的 store 和 NCCL 可 bootstrap 的网络；跨 WAN 需要额外设计。

### torchft / PyTorch

- torchft 用 handle/ replica 顺序驱动 reconfigure；
- PyTorch c10d PR #189359 的 fresh-init 路径：revoke、drain、abort，再用
  uuid-namespaced PrefixStore 重建 communicator；
- PR #199240 的 fast path：quorum shrink + grow，rank 顺序有约束，
  不满足时回退 fresh init；
- 两者都 closed、未 merge；当前可用路径是 torchcomms。

## 3. 吸收矩阵

| 来源 | 机制 | 我们吸收什么 | 我们不改写什么 |
|---|---|---|---|
| DynaTrain | VPS、几何交集、rank-local transfer、setup overlap | 布局代数（A2）和 transfer plan | 不重写并行切换/调度器 |
| ElasWave | per-step snapshot、RNG resharding、dynamic communicator | 快照/RNG/step 语义 | 不采用 async 训练语义；communicator 用 torchcomms |
| ETC | 直接 P2P、cost-matrix planner、d−1 容错 | transport provider、成本模型 | 不重写 GPU P2P |
| torchcomms / torchft | reconfigure、shrink/grow、quorum | 直接依赖 | **不自己写 NCCL grow/shrink** |
| ByteCheckpoint / DCP | data/metadata 解耦、online resharding、DTensor 元数据 | state provider、manifest | 不重写保存/加载引擎 |
| TierCheck / FastCheck | 分层快照、本地快速恢复 | 本地快照层 | 不重写存储系统 |
| Tenplex | parallel tensor collection | 对比 layout 抽象 | 不重复其运行时 |
| gVisor/nvcf CRIU | 进程级 GPU 恢复 | 兜底思路 | 不作为主路线 |

## 4. 我们真正要攻的

1. **stable-step 状态闭包**：参数、Adam m/v、RNG、grad scaler、data loader、
   grad accumulation；在 step 边界一致抓取/恢复。
2. **布局代数**：TP/PP/DP/ZeRO/DTensor 的任意 M→N 重分片；range 级映射，
   不物化全量。
3. **state × reconfigure 的时序**：freeze → reconfigure → target 安装 state →
   同一个 step 的第一次 collective；顺序错一步 loss 就不一致。
4. **跨集群 store/rendezvous**：torchcomms 的 TCPStore 在 A/B 之间是否可达；
   不可达时的 proxy rank / 分层 communicator。
5. **fence/commit/proof**：lease/epoch/quorum/coverage；source death 下的
   fail closed 或有限回滚。

## 5. 我们不宣称的

- 不宣称“第一个给 NCCL 加 rank”；
- 不宣称“第一个做 elastic training”；
- 不宣称“communicator 是我们发明的”；
- 不把 simulator milestone 当技术攻关；
- 不在没有真实 PyTorch/NCCL/state 的情况下宣称“端到端”。

## 6. 与代码的映射

- `lrs/layout.py`（A2）：VPS-lite 布局代数；
- `lrs/state_closure.py`（A3）：真实 state 闭包；
- `lrs/transport.py`（M3）：跨域 delta transport；
- `lrs/tp_pp.py`（M5）：TP/PP toy；
- 未来 `lrs/reconfigure.py`：torchcomms adapter；
- 未来 `lrs/transaction.py`：state × reconfigure 的 commit 编排。

## 7. 参考

- NCCL `src/nccl.h.in`（grow/shrink/suspend/revoke）
- torchcomms `comms/torchcomms/nccl/TorchCommNCCLReconfigure.cpp`
- PyTorch PR #199240、#189359（closed、未 merge）
- ElasWave arXiv 2510.00606
- DynaTrain arXiv 2605.18815
- ETC arXiv 2607.04749
