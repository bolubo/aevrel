# LRS 4-GPU Runbook

状态：前置脚本已就绪（2026-10-22）；待租 4 卡窗口
适用：单节点 4×GPU、CUDA 13、PyTorch 2.14.1+cu130、NCCL 2.30.7
环境：`.venvs/lrs-gpu`（已装 torch 2.14.1+cu130 + numpy 2.5.4）

## 1. 单卡已完成

- 单卡：RTX 5070 Ti Laptop 12GB；
- `SINGLE GPU SMOKE PASSED`；
- full grad diff `<= 8.9e-16`；
- TP/PP shard diff `<= 2.8e-17`；
- 证据：`evidence/lrs/2026-10-22-gpu/smoke.log`。

单卡不能跑 4 个 rank；4 卡窗口只验证 NCCL/TP/PP 基线，不重复单卡 smoke。

## 2. 租卡前置

- 4×GPU 在同一节点（L20/A800/4090 均可，显存足够即可）；
- NVIDIA 驱动支持 CUDA 13；
- 不需要共享文件系统；
- 建议一次窗口 30–60 分钟，先跑 probe 和 TP/PP baseline，再做 rank handoff。

## 3. 窗口步骤

### 3.1 预检

```bash
nvidia-smi -L
.venvs/lrs-gpu/bin/python -c \
  "import torch; print(torch.__version__, torch.cuda.device_count())"
```

期望：4 个 GPU，`torch.cuda.device_count() == 4`。

### 3.2 NCCL probe

```bash
cd /root/lines/oss/aicluster/shardferry-lrs
.venvs/lrs-gpu/bin/torchrun --standalone --nproc_per_node=4 \
  scripts/lrs_nccl_probe.py
```

期望：`NCCL PROBE PASSED`，`all_reduce_max_diff=0`。

如果失败：

- 检查 `NCCL_SOCKET_IFNAME` / 多网卡；
- 检查 `NCCL_IB_DISABLE`（无 InfiniBand 时设 1）；
- 检查驱动与 torch cu130 是否匹配。

### 3.3 TP=2/PP=2 baseline

```bash
.venvs/lrs-gpu/bin/torchrun --standalone --nproc_per_node=4 \
  scripts/lrs_nccl_tp_pp.py
```

期望：`NCCL TP/PP BASELINE PASSED`，
`max_grad_diff <= 1e-8`，`max_loss_diff <= 1e-12`。

这一步验证：

- stage 0 的 TP all-gather；
- stage 1 的 TP all-reduce；
- stage 0/1 的 PP send/recv；
- backward 的 da all-gather；
- TP/PP 分片梯度与 full-model baseline 一致。

### 3.4 rank handoff（下一步脚本）

4 卡单节点没有空闲 GPU，真正的 A→B rank 迁移需要：

- 8 卡（4 source + 4 target），或
- 2 节点各 4 卡。

4 卡窗口内可先做 **同节点 rank restart/handoff**：

1. rank 1 保存当前 shard；
2. rank 1 退出；
3. 新进程在 GPU 1 上加载 shard；
4. 重建 NCCL communicator；
5. 其余 3 个 rank 继续，比较 loss/权重。

这验证“状态交接 + communicator 重建”，但不是跨集群 A/B；
跨集群 A/B 的 TP/PP 需要 8 卡或 2 节点窗口。

## 4. 验收记录

窗口内至少记录：

- GPU 型号/驱动/torch/NCCL 版本；
- probe 输出；
- TP/PP baseline 的 `max_grad_diff`、`max_loss_diff`；
- 每步耗时、collective 耗时；
- rank handoff 的停机时间、恢复时间、权重差异；
- 失败时的完整 stderr。

## 5. 当前脚本

- `scripts/lrs_nccl_probe.py`：4-rank all-reduce + matmul probe；
- `scripts/lrs_nccl_tp_pp.py`：4-rank TP=2/PP=2 baseline；
- `scripts/lrs_gpu_smoke.py`：单卡 CUDA/TP-PP math smoke。

rank restart/handoff 脚本待窗口前补。
