# Single-GPU validation (2026-10-22)

状态：真实单卡 GPU 验证完成；不是多 rank / 多节点验证。

## 机器

- GPU: NVIDIA GeForce RTX 5070 Ti Laptop GPU, 11.94 GiB
- Driver: 592.01, driver-reported CUDA 13.1
- PyTorch: `2.11.0+cu128`
- CUDA runtime in torch: 12.8

## L3 target-ready

使用 FSDP 单 rank 保存 DCP，再用新进程加载模型和 AdamW optimizer：

- target 加载 DCP；
- 模型输出与 source fixture bitwise 一致（fp32）；
- target 真实运行 10 个训练 step；
- `shardferry-target-report` 成功写出 `modelLoaded=true`、
  `optimizerLoaded=true`、`stepsRan=10`。

命令入口：

```text
scripts/gpu_source_save.py
scripts/gpu_single_target_ready.py
```

## Handoff 闭环

本地版本目录：

```text
source/step-100/  # 初始 DCP
source/step-200/  # pause 期间生成的最终 DCP
```

流程：

1. target follower warm-follow 到 `step-100`；
2. `cutover` 调用 pause hook；
3. pause hook 创建 `step-200`；
4. final delta + target shadow materialize；
5. promote `step-200`；
6. target GPU 进程从 `target/shadow/step-200` 加载；
7. 运行 5 个训练 step 并写 report。

本地单卡结果：

- state: `promoted`
- version: `step-200`
- target current: `step-200`
- target-ready: `modelLoaded=true`, `optimizerLoaded=true`, `stepsRan=5`
- 本机 `paused_seconds`: 3.35s（包含 target 进程启动和 GPU 加载，不是生产 RTO）

## 证据边界

- 单 rank / 单 GPU；
- 没有 4→3、4→2 或 128 rank；
- 没有真实 Kubernetes target Job / 多节点网络；
- 不能把本结果当作 GPU 集群 RTO 或生产 SLA；
- 它证明真实模型的模型+优化器恢复和训练 step 可以完成。
