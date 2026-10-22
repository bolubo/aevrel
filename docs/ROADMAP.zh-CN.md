# shardferry 路线图

## 版本规则

路线图按 SemVer 编号：

- **0.1.x**：静态 checkpoint 迁移的维护线；
- **0.2.0**：训练热接力（Live Handoff）大版本；
- 口语中的“0.11”不再作为版本号使用；如果指“第一次可玩的 rescue 体验”，
  它是 0.1.x 的一个体验补丁，不单独成为大版本。

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

- 在线 cutover / live handoff；
- 新 checkpoint 格式；
- 新调度器或训练框架；
- 大规模重写数据面。

0.1 的完整验收见 [0.1 目标与验收](0.1-ACCEPTANCE.zh-CN.md)。

## 0.1.x 可选体验补丁：Rescue Demo

这不是 0.2，也不阻塞 0.2：

- 两套 kind、无 GPU；
- 一条命令启动 16→8 本地盘迁移；
- 注入 target agent 失败、controller 重启；
- 显示 journal 恢复、重传字节、target load；
- 输出 timeline 和 proof。

它服务于首次体验和 CI onboarding，不改变“静态 checkpoint 迁移”的产品边界。

## 0.2.0：Live Handoff（训练热接力）

### 用户任务

> source 训练任务还在写 checkpoint；target 已准备好不同 world size 的 shadow；
> 在明确的 checkpoint 边界暂停 source，让 target 接管并继续训练。

0.2 不是把 CUDA 进程搬过去，而是：

```text
source keeps training
      -> target follower catches up committed versions
      -> source pauses at a checkpoint boundary
      -> final delta + fence + promote
      -> target loads and continues for N steps
      -> source can stop; failure rolls back to source
```

### 0.2 必须交付

- source checkpoint version 协议和持续 follower；
- target 不同 world size 的 shadow；
- `shardferryctl handoff -f FILE`；
- checkpoint boundary、pause budget、fence、final delta；
- promote / rollback / stale fence / duplicate handoff 的明确状态；
- target 训练 harness 至少继续 N 个 step；
- 4×L20 上真实 `4→3` 或 `4→2` live handoff；
- 128→64 CPU/控制面规模验证；
- timeline、pause/RPO、proof、source digest 证据。

### 0.2 明确不做

- CUDA context / Python 堆的内存在线迁移；
- 任意 tensor/pipeline parallel 布局自动转换；
- 未保存 dataloader/RNG/scheduler 时的“完全相同训练轨迹”承诺；
- 生产 SLA 和任意公网条件下的 RTO 承诺。

详细门槛见 [0.2 目标与验收](0.2-ACCEPTANCE.zh-CN.md)。

## 0.2.x：Live Handoff 稳定化

在不改变 0.2 语义的前提下补：

- 更多 GPU 拓扑和真实多节点；
- 网络整形、带宽退避和 backpressure；
- dataloader/RNG/scheduler 的可选状态扩展；
- 更丰富的 proof、timeline 和故障注入；
- 训练平台/Kueue 的显式 handoff 集成。

## 0.3：自动迁移策略（Autopilot）

从“用户显式 handoff”提升到“平台策略触发 handoff”：

- spot/节点故障/集群升级事件；
- target capacity 和兼容性预检；
- 迁移策略、配额、审批和冷却；
- 自动 target job 启动与验证；
- 失败后的人工接管路径。

自动发现和自动触发必须建立在 0.2 的显式 handoff 已经可靠之上。

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

1. 完成 0.1.x 的发布维护和真实安装验证；
2. 完成 0.2 的 source version 协议、follower、fence 和 target-ready；
3. 跑通一次真实 GPU live handoff；
4. 再补 0.1.x Rescue Demo 和 0.2 的故障/规模矩阵。

不在当前优先级：更多格式、更多 provider、模型推理和通用工作负载迁移。
