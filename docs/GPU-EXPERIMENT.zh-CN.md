# shardferry GPU/FSDP 实验卡

GPU 还没有接入前先准备好，GPU 会话只做验证，不做探索。

两小时逐步操作见 [GPU 两小时验收 Runbook](GPU-RUNBOOK.zh-CN.md)。

## 最低机器条件

- 单机至少 4 张 CUDA GPU，能同时运行 4 个训练进程；
- CUDA 版 PyTorch，`torchrun`、NCCL 可用；
- source 4 rank 和 target 3 rank 使用同一套代码和依赖；
- 预留 source/target checkpoint、DCP work 和日志空间；
- 记录 GPU 型号、显存、驱动、CUDA、torch、NCCL 版本。

如果 GPU 是租用资源，按现有红线先列创建清单，结束后全删并核对次日账单。

## 本仓库提供的测试脚本

- `scripts/gpu_source_save.py`：4-rank FSDP + AdamW 保存 DCP。
- `scripts/gpu_target_load.py`：目标 rank 数加载 DCP，恢复 model/optimizer，
  对固定输入做 forward 对比。
- `scripts/gpu_preflight.py`：只读检查 GPU、CUDA、NCCL、torchrun、磁盘和版本。

这些短脚本在没有 CUDA 时会明确退出，不会误跑 CPU；实验按 Runbook 逐个命令执行，
不再使用长编排脚本。

## 正常流程

### GPU 机器 source

```bash
torchrun --standalone --nproc_per_node=4 \
  scripts/gpu_source_save.py \
  --out /data/sf-gpu-source \
  --fixture /data/sf-gpu-source.fixture.pt \
  --dtype float32
```

### GPU 机器 target

```bash
torchrun --standalone --nproc_per_node=3 \
  scripts/gpu_target_load.py \
  --ckpt /data/sf-gpu-source \
  --fixture /data/sf-gpu-source.fixture.pt \
  --dtype float32
```

成功条件：

- target 两个/三个进程都输出 `output_ok=True optimizer_state_loaded=True`；
- 权重、optimizer state 加载无形状/键名错误；
- forward 输出默认 `torch.equal`；如果 GPU 非确定性导致不等，不要静默放宽容差，
  先记录算法、TF32 和 deterministic 设置，再决定是否接受；
- 记录 bytes、时间、显存峰值和 DCP 文件大小。

## 必测矩阵

| 用例 | source | target | 检查 |
|---|---|---|---|
| fp32 4→3 | 4 GPU/rank | 3 GPU/rank | model + AdamW + forward |
| fp32 4→1 | 4 | 1 | target 全量加载 |
| 1→4 | 1 | 4 | 广播/重分片 |
| bf16 4→3 | 4 | 3 | dtype 和输出 |
| 中断续跑 | 4 | 3 | journal、source/target 不变 |
| source 重启 | 4 | 3 | HTTP 失败后恢复 |
| 8→5、16→7 | 多卡 | 多卡 | bytes、内存、时间 |

只有在 GPU 上跑通的用例才写入结果；脚本存在不等于已验证。

## GPU 前的 CPU 门禁

1. 完整 CPU 测试全绿；
2. 物理 A/B 正常路径和断网恢复有独立证据；
3. `gpu_source_save.py --help`、`gpu_target_load.py --help` 通过；
4. DCP adapter 对多 tensor、optimizer-like state、target Partial 有测试；
5. 交接包 hash 固定，GPU 会话不改代码；
6. 资源清单和清理责任人明确。

## 不要做

- 不要在 GPU 会话里临时改拓扑或临时写对比脚本；
- 不要把 CPU 测试的通过当成 GPU 通过；
- 不要把 `torch.allclose` 当成 bitwise；
- 不要在没有记录环境版本的情况下比较不同 GPU/CUDA 的结果。
