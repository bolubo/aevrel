# shardferry 控制器 GPU 实验卡

状态：准备中  
定位：控制面验收，不是 FSDP 数据面矩阵  
输入：复用 [GPU/FSDP 实验卡](GPU-EXPERIMENT.zh-CN.md) 产出的 4-rank
FSDP checkpoint、fixture 和环境元数据。

## 1. 不重叠边界

数据面 GPU 实验负责：`gpu_source_save.py`、`gpu_target_load.py`、
4→3/4→1/1→4、bf16、8→5/16→7、source 中断和目标 load 正确性。

本卡只验证控制器：

- `CheckpointMigration` CRD、controller、source/target agents、gateway；
- rank 发现、计划、commit barrier、status/report/proof；
- controller 进程中断后的恢复；
- real GPU target 的 cutover pause/promote/target-ready；
- GPU 节点上的 Helm 调度和节点故障边界。

不重新生成 FSDP checkpoint，不重跑数据面矩阵，不修改数据面脚本。

## 2. 控制器真正需要 GPU 的地方

### C1：真实 GPU DCP 的控制面闭环（P0）

用数据面实验已经生成的 `/data/sf-gpu-source` 作为只读 source，在一个
4-GPU 节点或两节点小集群中安装 controller + source/target agents，运行
4 source ranks → 3 target ranks 的 `CheckpointMigration`。

验证：

- `preflight` 发现 4/4 source ranks 和 3/3 target ranks；
- CR 从 `preparing` 到 `succeeded`，3/3 target rank committed；
- source SHA256 前后不变，plan 不产生 full gather；
- report/proof 和 per-rank status 可读取；
- 最终 target load 由数据面实验的 `gpu_target_load.py` 验收，不在本卡重复。

GPU 在这里的作用是提供真实 FSDP metadata、optimizer state 和 target
readiness；controller 本身仍不需要 CUDA。

### C2：单节点多 rank / local disk（P0，可并入 C1）

同一个 source agent 服务同一节点上的 4 个 rank，target agent 服务 3 个
target rank；不写手写 `rankMap`，由 selector 发现 Pod 和本地 rank。

验证 controller 不把“一 Pod 一 rank”当前提，source/target 都能从 hostPath
local disk 读取和写入 `.distcp`。

### C3：controller 中断恢复（P0，复用 C1 的 checkpoint）

在 transfer 或 commit barrier 前删掉 controller Pod，让新 Pod 从 CR、
execution snapshot 和 artifact 恢复；迁移最终仍成功，且不重复已提交 rank。

这是控制器专属测试，不重复数据面 source 重启或数据面断点续跑。

### C4：真实 GPU training 的 cutover（P1）

用一个持续写入 checkpoint 版本的 4-GPU 小训练进程，验证：

- source pause hook 在 checkpoint 边界暂停；
- final delta 追赶；
- target GPU job 通过 readiness hook 后 promote；
- 失败时 rollback/resume source；
- `/v1/status` 记录 `paused_seconds`、`target_ready_seconds` 和版本 ID。

这项验证控制器的在线切换编排，不比较 FSDP 数据面正确性；正确性仍由
数据面 target load 结果背书。

### C5：GPU 节点调度（P1）

在带 GPU taint、label 的节点上验证 source/target agents 和 controller 的
`nodeSelector`、`tolerations`、`affinity`。Helm chart 已支持这些字段；
kind 模拟不能替代真实 GPU 节点标签和 taint。

示例 values 片段：

```yaml
source:
  nodeSelector:
    nvidia.com/gpu.present: "true"
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
target:
  nodeSelector:
    nvidia.com/gpu.present: "true"
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
```

## 3. 不需要 GPU 的控制器部分

以下继续走 CPU/kind/单元测试，不占用 GPU：

- planner、layout compiler、目标 shard inventory；
- CRD/schema、commit barrier、cancel/retry/delete；
- artifact store、retention、GC、mTLS、gateway 反向代理；
- 错误分类、重试预算、events、metrics、status；
- 目标 DCP 的 CPU load smoke。

不做 CUDA 算子、NCCL 调优、GPUDirect、CRIU/cuda-checkpoint；这些不属于
当前 V1 的控制器边界。

## 4. 建议的 GPU 时间顺序

1. **Run A（P0）**：单机 4 GPU，复用数据面实验的 source checkpoint；
   C1 + C2 + C3 一次装好、连续跑完。先只跑一次，不按每个用例重启集群。
2. **Run B（P1）**：同一台机器上跑真实 training cutover（C4）；如果 source
   checkpoint 产生脚本需要新增，先和数据面实验对齐后再动。
3. **Run C（P1）**：有第二台 GPU 节点时再跑跨节点 local disk 和节点故障；
   没有第二台就不把跨节点写成已验证。

如果 GPU 是租用资源，按全局红线先写创建清单（资源/区域/计费/时间），
结束后全删并核对次日账单；余额低于 ¥150 不创建按量资源。

## 5. 最少证据

- `kubectl get checkpointmigration -o yaml` 的关键 phase/counts/ranks；
- `describe migration --ranks`、report、proof；
- source 前后 SHA256；
- controller 重启前后的 executionRef、phase、completed rank；
- 数据面实验的 target GPU load 输出；
- GPU 调度测试的 `kubectl get pods -o wide`；
- cutover 的 paused/target-ready 时间和失败回滚记录。

只有实际跑过并留下上述证据的项才写“已验证”；脚本存在不算证据。
