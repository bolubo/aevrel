# LRS 4-GPU Runbook

状态：CPU4 前置通过；1-rank torchcomms reconfigure 通过；待租 4 卡窗口
（2026-10-22）
适用：单节点 4×GPU、CUDA 13、NCCL 2.30.7
环境：

- `.venvs/lrs-gpu`：torch `2.14.1+cu130`，静态 NCCL/TP/PP baseline；
- `.venvs/lrs-gpu-comm`：torch `2.15.0.dev20260824+cu130` +
  torchcomms `0.3.0.dev20260824+cu130`，动态 reconfigure；
- 不要把 torchcomms 装进 `.venvs/lrs-gpu`：0.3.0.dev 与 torch 2.14.1
  的 Python/C++ 绑定不兼容，已实测报 `RedOpType is already registered`。

## 1. 单卡已完成

- 单卡：RTX 5070 Ti Laptop 12GB；
- `SINGLE GPU SMOKE PASSED`；
- full grad diff `<= 8.9e-16`；
- TP/PP shard diff `<= 2.8e-17`；
- 证据：`evidence/lrs/2026-10-22-gpu/smoke.log`。

单卡不能跑 4 个 rank；4 卡窗口只验证 NCCL/TP/PP 基线，不重复单卡 smoke。

## 2. CPU4 前置已完成

`scripts/lrs_cpu4_live.py` 已用真实 gloo 完成 4→3→4：

- 20 步 baseline vs 5 步 + 5 步 + 10 步 live；
- rank1 退出后，0/2/3 接管其样本，global batch 保持 16；
- rank1 sampler/model/optimizer 状态交给 target；
- `param_diff=1.388e-17`、`loss_diff=0`、sampler 完全一致。

它仍是 `destroy/reinit` 的分阶段切换，`global_stop=true`，不是最终
live collective。详见 [LRS-A5-CPU4](LRS-A5-CPU4.zh-CN.md)。

## 3. 租卡前置

- 4×GPU 在同一节点（L20/A800/4090 均可，显存足够即可）；
- NVIDIA 驱动支持 CUDA 13；
- 不需要共享文件系统；
- 建议一次窗口 30–60 分钟，先跑 probe 和 TP/PP baseline，再做 rank handoff。

## 4. 窗口步骤

### 4.1 预检

```bash
nvidia-smi -L
MASTER_ADDR=$(hostname -I | awk '{print $1}')
echo "MASTER_ADDR=$MASTER_ADDR"

.venvs/lrs-gpu/bin/python scripts/lrs_gpu_preflight.py
.venvs/lrs-gpu-comm/bin/python scripts/lrs_gpu_preflight.py \
  --require-torchcomms
```

期望：两个 venv 都输出 `GPU PREFLIGHT PASSED`；第二个还输出
`torchcomms=...`。同时确认 `nccl_world1_all_reduce=1.0`。

### 4.2 NCCL probe

```bash
cd /root/lines/oss/aicluster/shardferry-lrs
MASTER_ADDR=$(hostname -I | awk '{print $1}')
.venvs/lrs-gpu/bin/torchrun --nnodes=1 --nproc_per_node=4 \
  --node_rank=0 --master_addr="$MASTER_ADDR" --master_port=29511 \
  scripts/lrs_nccl_probe.py
```

期望：`NCCL PROBE PASSED`，`all_reduce_max_diff=0`。

如果失败：

- 检查 `NCCL_SOCKET_IFNAME` / 多网卡；
- 检查 `NCCL_IB_DISABLE`（无 InfiniBand 时设 1）；
- 检查驱动与 torch cu130 是否匹配。

### 4.3 TP=2/PP=2 baseline

```bash
MASTER_ADDR=$(hostname -I | awk '{print $1}')
.venvs/lrs-gpu/bin/torchrun --nnodes=1 --nproc_per_node=4 \
  --node_rank=0 --master_addr="$MASTER_ADDR" --master_port=29512 \
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

### 4.4 torchcomms reconfigure probe

先用 1-rank 版本验证动态 communicator API；4 卡窗口把 `nproc_per_node`
改成 4，跑 full world reconfigure：

```bash
MASTER_ADDR=$(hostname -I | awk '{print $1}')
.venvs/lrs-gpu-comm/bin/torchrun --nnodes=1 --nproc_per_node=4 \
  --node_rank=0 --master_addr="$MASTER_ADDR" --master_port=29513 \
  scripts/lrs_torchcomms_probe.py
```

1-rank 已在本机通过：

```
world=1 reconfigure_seconds=0.654
TORCHCOMMS RECONFIGURE PROBE PASSED
```

4 卡窗口必须记录 `reconfigure_seconds`、是否 hang、all-reduce 数值。

### 4.5 rank handoff（下一步脚本）

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

## 5. 验收记录

窗口内至少记录：

- GPU 型号/驱动/torch/NCCL 版本；
- probe 输出；
- TP/PP baseline 的 `max_grad_diff`、`max_loss_diff`；
- 每步耗时、collective 耗时；
- rank handoff 的停机时间、恢复时间、权重差异；
- 失败时的完整 stderr。

## 6. 当前脚本

- `scripts/lrs_nccl_probe.py`：4-rank all-reduce + matmul probe；
- `scripts/lrs_nccl_tp_pp.py`：4-rank TP=2/PP=2 baseline；
- `scripts/lrs_gpu_smoke.py`：单卡 CUDA/TP-PP math smoke。
- `scripts/lrs_gpu_preflight.py`：单进程 NCCL/backend/torchcomms 预检；
- `scripts/lrs_torchcomms_probe.py`：动态 reconfigure + all-reduce probe；
- `scripts/lrs_cpu4_live.py`：CPU 4-rank 4→3→4 前置闭环。

rank restart/handoff 脚本待窗口前补；在它之前先跑通 4.4 的 reconfigure
probe，否则不要直接进入状态交接。
