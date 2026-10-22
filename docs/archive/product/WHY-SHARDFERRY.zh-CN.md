# 为什么是 shardferry

## 不要把它理解成“另一个 DCP”

PyTorch DCP 已经能保存、加载和在不同拓扑上 reshard。问题不在于
`torch.distributed.checkpoint` 不会处理 DCP，而在于它默认：

> checkpoint 数据已经在某个能被 reader 看到的路径下。

当 128 个 rank 各自的 shard 分散在不同节点的本地盘、源训练进程已经退出、
目标集群只有 64 张卡时，DCP 本身没有负责：

- 找到每个节点上的 shard；
- 建立跨节点 range 传输；
- 发现 source/target rank；
- 失败后的 journal/resume；
- 跨节点的 staging/commit barrier；
- 生成可验证、可审计的迁移报告。

shardferry 补的是这一层。

## 典型用户

### 1. AI 平台 / 基础设施工程师

这是第一用户。

- 管理 8–128 卡训练集群；
- Kubernetes + PyTorch FSDP/DTensor；
- 节点有本地 NVMe，没有足够大的共享存储；
- 需要处理任务失败、节点回收、集群升级和迁移；
- 关心 source 不变、target 能 load、失败能恢复。

### 2. 训练任务负责人

- 有 DCP checkpoint，需要换 world size 或集群；
- 不熟悉 DeviceMesh 和 rankMap；
- 希望一条命令和一个简短 YAML 完成迁移；
- 最终只关心 target 训练任务能不能正常加载。

### 3. 集群运维 / 灾难恢复团队

- 负责节点下线、机房切换和灾备；
- 需要迁移报告、bitwise 证据、失败 rank 和恢复记录；
- 需要可重复、可审计的操作，而不是一次性脚本。

## 与其他方案的区别

| 方案 | 强项 | 不负责的部分 |
|---|---|---|
| PyTorch DCP | DCP 格式、save/load、load-time reshard | local-disk 跨节点发现、传输、恢复和产品化执行 |
| DeepSpeed / Megatron / NeMo | 与各自训练框架深度集成 | 通用 DCP local-disk 迁移；通常绑定框架/格式 |
| checkpoint 转换脚本 | 针对单一项目快速处理 | 可运维性、失败恢复、审计报告 |
| NFS / CephFS / JuiceFS | 提供共享命名空间 | 不解决拓扑转换和迁移语义 |
| rsync / rclone / 对象存储拷贝 | 搬字节 | 不理解 DCP tensor、rank、mesh，也不做 target DCP |
| CRIU / cuda-checkpoint | 进程/GPU 现场恢复 | 重依赖硬件和权限，不是可移植 DCP shard 迁移 |
| Kubeflow / Kueue / Ray | 作业编排和调度 | 不负责 local checkpoint shard 的跨节点搬运 |
| 备份/卷快照 | 卷级别恢复 | 不理解 DCP 拓扑，不能直接生成目标 DCP |

## 不该用 shardferry 的场景

以下情况应该直接使用原生 DCP 或现有存储方案：

- 所有 shard 已经在同一个共享目录；
- 源训练任务仍然活着，DCP load 可以直接完成；
- 只需要异步 save checkpoint；
- 需要通用模型格式转换；
- 需要模型 serving 或训练调度。

我们不把“共享盘 + DCP”包装成 shardferry 的卖点。

## 我们的核心差异

1. **local-disk first**：不要求先汇总到全局存储。
2. **topology-aware**：按目标 world size、mesh 和 placement 设计传输。
3. **transactional**：staging、bitwise、source 只读、all-or-nothing commit。
4. **recoverable**：journal、幂等 task、controller/agent 重启续跑。
5. **operator-friendly**：selector 发现 agent 和本机 rank。

这五点合在一起，才是 shardferry 的位置。
