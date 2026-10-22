# shardferry 路线图

## 阶段 A：Alpha 本地闭环（当前）

目标：一个人能在单机或共享盘上完成一次真实 DCP 迁移，并知道结果是否正确。

- `demo`、`doctor`、`migrate` 三条命令；
- 真实 DCP 4→3；
- source range read，无全局 gather；
- target DCP export；
- bitwise verification；
- 失败后重跑和 journal 续跑；
- wheel、Dockerfile、Helm chart 和公开文档。

验收：Python/Go/Helm 全绿；真实 DCP 加载；源目录始终不变。

## 阶段 B：Kubernetes local-disk 可用闭环

目标：16→8 小集群 selector 迁移可以交给平台用户试用。

详细目标与验收标准见 [0.2 目标与验收](0.2-ACCEPTANCE.zh-CN.md)。

- controller + source/target agents 的真实 kind 或小集群部署；
- `agentSelector` 自动发现 Pod、rank 和 endpoint；
- `preflight` 真实报告 source/target rank 覆盖；
- target rank 本地 DCP finalize 和 load smoke；
- target 节点故障、source 节点故障、controller 重启的恢复证据；
- 128→64 CPU 规模仿真。

验收：用户只写一份 YAML，不手写 rankMap；失败时不误报成功。

## 阶段 C：生产级 local-disk 迁移

目标：真实训练团队可以在故障恢复或集群替换时使用。

- 真实 128-rank local-disk 多节点；
- GPU/FSDP 模型和 optimizer load；
- 1GB/10GB+ checkpoint、磁盘空间和内存门禁；
- mTLS、token、RBAC、NetworkPolicy；
- S3/PVC artifact 生命周期和 GC；
- 可观测指标、事件、报告和运维 runbook。

验收：有可重复的故障矩阵和规模数据，不靠口头承诺。

## 阶段 D：在线 cutover / CDP（实验到可选）

只有在阶段 C 稳定后，才把持续 shadow follower、pause、promote、rollback
和 RTO/RPO 作为可选能力暴露给用户。

## 长期品类演进（不影响当前交付）

DCP 是第一个 vertical，不是最终品类。当前阶段的扩展顺序是：

1. **DCP state provider**：把 local-disk checkpoint 迁移打穿；
2. **通用 state provider**：文件、volume、dataset、model artifact；
3. **workload provider**：manifests、镜像、配置、依赖和接线；
4. **迁移判定与规划层**：判断能不能迁、迁移闭包、最快安全路径和成本；
5. **跨控制域迁移**：本地多集群、云上云下、灾备和计划性扩缩容；
6. **在线 cutover**：停顿窗口、增量追赶、promote/rollback。

叙事可以放大，交付范围不能放大。只有当当前 vertical 的真实闭环稳定后，
才把下一层纳入主线。

## 开发优先级

1. 目标训练 load smoke；
2. 真实 selector 多节点闭环；
3. 128→64 规模和恢复；
4. 镜像/安装/可观测性；
5. 在线 cutover。

不在当前优先级：旧测试数量、无关的 e2e harness、历史开发记录、更多格式支持。
