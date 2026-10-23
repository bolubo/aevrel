# shardferry 路线图

状态：方向性路线图，不承诺日期或版本内容。

## 当前位置

项目已经有一个可运行的控制面/数据面基础，但下一阶段不是继续堆功能，而是把项目
建设成可维护、可体验、可贡献的仓库。

当前建设阶段见 [项目建设方向](CONSTRUCTION.zh-CN.md)：

1. **UI/UX**：让用户看懂控制面、状态、动作和上下游关系；
2. **测试覆盖**：覆盖关键语义、失败边界和真实用户路径；
3. **重构**：明确 Go/Python/Rust/C 边界，整理包、目录和依赖方向。

这三项完成之前，不把新功能数量当作进度。

## 版本策略

- 使用 SemVer；
- pre-release 明确标注，不能描述为生产稳定版；
- 版本发布由实际可用性和验证证据决定，不由日期决定；
- 未跑过的 GPU、多节点、Kubernetes 或跨集群验证必须写成待验证；
- 任何版本都应保持 source 只读、fail-closed 和 proof 可复核。

## 后续产品方向

以下方向在建设阶段完成后重新评估，不构成当前承诺：

### Provider / scheduler 互操作

- 更多 checkpoint、存储、训练框架 provider；
- Kueue、Volcano、Kubeflow 等 scheduler consumer；
- 稳定的 readiness/rescue/mobility 信号。

### 跨控制域状态移动

- local-disk、无共享存储的跨集群移动；
- 成本和兼容性决策；
- 更完整的 proof、timeline 和失败恢复。

### 在线拓扑迁移

- LRS 的 GPU/NCCL 连续性和 2 节点验证；
- `maxInFlight`、quorum、source-death takeover；
- 与 static migration 保持明确边界。

### 嵌入式控制层

- 可被平台默认集成的 CRD/API/library；
- 多 provider、多 consumer 的稳定契约；
- 1.0 所需的生产边界、升级策略和安全证据。

## 不做什么

- 不重写 Kueue/Volcano/Kubeflow；
- 不重写 DCP save/load；
- 不定义新的 checkpoint 格式；
- 不承诺任意 CUDA/Python 热迁移；
- 不做没有动作和 proof 的“控制面大屏”；
- 不为没有第二个真实 consumer 的需求提前抽象。
