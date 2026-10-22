# shardferry 用户故事卡

这两张卡是版本边界，不是宣传文案。功能如果不能用这两张卡解释，就不应该
进入当前版本。

## 0.1：把已经结束的训练状态搬到新拓扑

### 用户

AI 训练平台的平台工程师 / 集群运维者。

### 场景

一个训练任务已经结束、被取消，或者 source 集群即将下线。checkpoint 是
多个 rank 写在各机器本地盘的 DCP；目标环境只有另一套 rank 数/DeviceMesh，
没有可共享的 NFS/S3 全局 checkpoint 目录。

### 用户原话式需求

> “我有一份已经写好的本地分片 checkpoint，不想先汇总到对象存储。  
> 目标训练任务卡数不一样，我要把它变成目标 rank 能加载的 DCP，并且确认
> source 没有被改。”

### 现在的做法

- 手动 rsync/复制到共享盘；
- 写脚本收集所有 rank 文件；
- 用 DCP 的原生加载重分片，但前提是目标能访问完整 checkpoint；
- 目标加载失败后，再从日志里猜是哪个 rank/哪个 shard 出了问题。

### shardferry 0.1 提供

```bash
shardferry doctor --checkpoint /path/source.dcp
shardferry migrate --checkpoint /path/source.dcp --target-world 64
shardferry-target-load --checkpoint /path/target.dcp --rank N --json
```

- source 只读；
- 直接读取 local `.distcp` range；
- source/target 不同 world size；
- staging、journal 恢复、bitwise verify、原子提交；
- target DCP 可加载。

### 成功定义

- source SHA256 前后不变；
- target rank 全部 load；
- 迁移失败不会报告成功；
- 可以从同一个 source 重新跑；
- 不需要完整全局 gather。

### 证据

- PyTorch DCP local-shard + separate DeviceMesh issue（#154787）；
- 4×L20 上的真实 DCP 迁移和 target load；
- 128→64 控制面/CPU 规模链路。

## 0.2：任务被抢占时，安全地恢复/迁移，而不是盲目重跑

### 用户

共享 GPU 集群的平台工程师；使用 Kueue/Kubernetes 管理训练任务；
同时有训练团队希望任务被抢占后不要白白丢失训练进度。

### 场景

高优先级任务需要 GPU，平台要抢占一个低优先级训练任务。该任务：

- 正在或刚刚写过 checkpoint；
- checkpoint 在本地盘，不是一个全局目录；
- target 可能是另一套卡数的集群；
- 平台需要在“继续等、直接重跑、恢复 checkpoint、迁移到 target”之间决策。

### 用户原话式需求

> “别只告诉我哪个任务优先级低。告诉我它最近有没有完整 checkpoint，
> 抢占会损失多少；如果要恢复，目标 Job 到底能不能加载成功。”

### 现在的做法

- Kueue 只按优先级/资源选受害者；
- workload 应用自己处理 SIGTERM 和 checkpoint；
- 平台不知道 checkpoint 是否完整、在哪、是否适合 target；
- 恢复经常变成“从零重启”或人工搬目录。

### shardferry 0.2 提供

三个功能共同组成一个故事：

1. **Checkpoint-aware preemption / restore**  
   平台能读取 checkpoint freshness/step/complete，参与抢占和恢复判断。
2. **Separate-Mesh local-shard migration**  
   需要迁移时，把本地分片搬到 target DeviceMesh，不要求共享 checkpoint 目录。
3. **Target-ready integration**  
   target Job/TrainJob 真实加载模型和优化器并运行 N 个 step，才报告 ready。

### 成功定义

- 平台不会把一个没有完整 checkpoint 的任务误判为可恢复；
- 能根据 freshness/中断成本选择抢占对象；
- target 不同 ranks 能加载；
- target 失败不会把迁移标记为成功；
- 记录 time-to-first-step、steps lost、bytes moved 和 proof。

### 证据

- Kueue checkpoint-aware preemption issue（#477）；
- KEP-5710 的 workload-aware preemption 和 WG Checkpoint/Restore；
- PyTorch DCP local-shard/跨 DeviceMesh 需求；
- Kubeflow Trainer checkpoint exporter / target-ready 讨论。

## 边界

- 0.1 是“静态状态搬迁”，用户已经接受任务停止；
- 0.2 是“抢占/恢复决策 + 跨拓扑搬迁 + target ready”，但**不是 CUDA
  进程热迁移**；
- Live Handoff 是后续高阶形态，不是这两张故事卡的替代品。
