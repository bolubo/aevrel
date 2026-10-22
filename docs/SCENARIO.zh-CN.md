# 场景 v0：显式触发的一次完整训练状态迁移

日期：2026-10-22
状态：问题定义，不是功能承诺

## 1. 一句话

用户在集群 A 中有一个已经落盘、可恢复的 DCP 训练状态，希望把它迁移到
集群 B，并按用户声明的目标资源/拓扑恢复；迁移由用户显式触发，不依赖故障
自动发现，也不要求共享文件系统。

## 2. 先定边界

### 属于 v0

- 一个训练任务、一个完整 DCP checkpoint；
- source 可以是多机 local disk，shard 分散在不同节点；
- target 的 GPU 数量/资源由用户声明，world size 可以与 source 不同；
- source 已停止或 checkpoint 已不可变；用户决定何时迁移；
- 迁移后 target 训练任务能 load；
- source 不被修改；
- v0 不负责调度/预留 GPU，也不负责启动 target 训练任务；用户提供目标 root
  和目标 job 的运行环境；
- 失败可恢复，不要求用户手写 rankMap；
- 两套 K8s 集群通过 gateway/明确 endpoint 通信，不要求 Pod CIDR 互通。

### 不属于 v0

- 自动发现故障并自动迁移；
- 只迁一个坏卡/一个 rank；
- 把一个集群里的所有训练任务一起迁；
- source 训练不停机、在线 cutover；
- 任意 checkpoint 格式；
- 数据库、对象存储、消息队列等非 DCP provider；
- 零停机承诺；
- 运行中训练的在线一致性快照（active writer）；
- 把迁移当成调度器或训练平台替代品。

## 3. 为什么不先做“只迁一张卡”

用户描述的“100 多张卡，一张坏了，只把那一张卡迁到另一个集群”听起来更省，
但它不是当前 DCP 迁移问题的子集：

1. 单个 rank 的 DCP shard 不是独立可加载的 checkpoint；目标训练要的是完整
   状态/完整拓扑，不是一张孤立的 shard。
2. 它更像 rank evacuation / runtime relocation，不是状态迁移。需要处理
   进程、rank 会话、通信组、优化器状态和训练任务生命周期。
3. 只迁一个 rank 不能直接得到 target 可 load 的结果；如果目标 world size
   变化，仍然要做整状态的 reshard。

所以 v0 先做“完整状态迁移”。部分 rank 迁移、在线迁移和故障自愈放到后续
能力，不和第一场景混在一起。

## 4. 用户实际填写什么

用户只声明意图和约束，不填写内部 rankMap、agent endpoint 或传输节点。

```yaml
source:
  clusterRef: cluster-a
  checkpointRef: /checkpoints/job42/step-1000
  worldSize: 128

target:
  clusterRef: cluster-b
  worldSize: 64
  resources:
    gpuType: A100
    gpuCount: 64
  root: /targets/job42

connectivity:
  mode: gateway          # gateway | direct | relay
  secretRef: cluster-b-auth

policy:
  objective: balanced    # fastest | lowest-cost | lowest-downtime
  verify: bitwise+load
```

这不是最终 CRD，而是用户意图的边界：

- source/target 集群；
- checkpoint 或任务引用；
- source/target world size；
- 目标资源和 root；
- 连接方式/凭据引用；
- 迁移目标和验证要求。

v0 默认 `source.consistency=immutable`：checkpoint 在迁移期间不能被继续写入。
如果 source 训练还在跑，先由用户/训练平台完成 checkpoint 并停写；在线快照
和 live cutover 不属于 v0。

## 5. 平台负责发现和决定什么

用户填完意图后，平台负责：

- 发现 source/target 的 agent、rank、gateway、版本和能力；
- 读取 DCP metadata，确认 tensor/chunk 和 source rank 覆盖；
- 检查 target 资源、磁盘、root 权限和目录冲突；
- 生成 exact-cover、无 full gather 的迁移计划；
- 选择直连/gateway/中继路径和并发策略；
- staging、bitwise 校验、commit barrier；
- 失败重试、resume、commit-incomplete repair；
- 输出 bytes、time-to-ready、峰值内存和 target load 证据。

这就是“声明式调控”：用户声明**要什么**，平台发现**有什么**，planner 决定
**怎么迁**。

## 6. “平滑迁移”的定义

v0 的平滑不是零停机，而是：

- 用户不需要写 128 条 rankMap；
- 用户不需要共享文件系统；
- source 不被修改；
- target 未通过校验就不算成功；
- 任一步失败都有 phase/rank/error code 和下一步动作；
- 中断后可以用同一意图继续；
- target 训练任务能在目标集群 load。

如果 source 训练必须持续运行，或需要秒级切换，那是 online cutover，不属于
v0。

## 7. 验收场景

### CPU / kind 控制面

- 两套 kind 集群；
- 16→8 完整迁移；
- 128→64 CPU/合成数据；
- 缺 rank、网络断开、gateway 重启、controller 重启；
- source 不变，target load，report 可读。

### GPU 一次性窗口

GPU 用于验证 DCP/FSDP 的真实 load，不用来替代控制面规模测试。一次 5–6 小时
窗口应覆盖：

- 4→2、4→3、2→4；
- fp32 和 bf16；
- 小模型和至少 1GiB/10GiB checkpoint；
- source save、migrate、target FSDP load、forward/optimizer 对比；
- 中断续跑和 target load 失败；
- 记录 GPU 型号、驱动、CUDA、torch、NCCL、RSS/显存和时间。

GPU 脚本只是执行器，不能把“脚本存在”当成已验证。

## 8. 当前不做

- 不把单卡迁移混入 v0；
- 不做自动发现/自动触发；
- 不把整个集群所有任务做批量迁移；
- 不在本阶段优化底层吞吐；
- 不扩第二个 provider；
- 不承诺零停机。
