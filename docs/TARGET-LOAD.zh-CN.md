# 目标 DCP 加载

这份文档解决迁移后的最后一跳：**目标训练任务能不能真正加载迁移结果**。

`aevrel` 的迁移成功只代表 target DCP 已经生成并通过 bitwise 校验；
用户还需要在目标训练环境里完成一次真实 load。

## 1. 先做模型无关的 smoke

在 target 节点的 Python 环境执行：

```bash
aevrel-target-load \
  --checkpoint /targets/job42/rank0 \
  --metadata-dir /targets/job42/metadata \
  --rank 0 \
  --json
```

成功输出类似：

```json
{
  "status": "ok",
  "mode": "rank-local",
  "tensors": 3,
  "bytes": 123456,
  "duration_seconds": 0.42,
  "peak_rss_bytes": 123456789
}
```

这说明：

- `.metadata` 可读；
- 该 rank 的 `.distcp` 文件可读；
- PyTorch DCP runtime 能反序列化本地 chunk；
- 文件大小、dtype 和 chunk 元数据没有明显错位。

它不是训练任务 load 的替代品，但能在用户启动训练前快速定位“文件/元数据/
rank”问题。

## 2. 全量 smoke

如果 target root 是一个完整 DCP 目录，可以不加 `--rank`：

```bash
aevrel-target-load \
  --checkpoint /targets/job42/full \
  --json
```

全量模式会为 metadata 中的每个 tensor 分配完整 CPU tensor，内存占用可能
很大。128-rank/大模型场景优先使用 rank-local smoke，再在真实训练进程里
用 DCP load 验证。

## 3. 在真实训练任务里 load

目标训练任务的 world size/DeviceMesh 必须与 target migration spec 一致。

普通 `nn.Module` 的最小形式：

```python
import torch.distributed.checkpoint as dcp

state_dict = model.state_dict()
dcp.load(state_dict, checkpoint_id=target_checkpoint)
model.load_state_dict(state_dict)
```

FSDP/DTensor 场景不要直接把所有 rank 的 metadata 当成普通 state dict。
使用 PyTorch 提供的 state-dict helper：

```python
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_state_dict,
    set_state_dict,
)

options = StateDictOptions(full_state_dict=False, cpu_offload=True)
model_state, optim_state = get_state_dict(model, optimizer, options=options)
dcp.load(
    {"model": model_state, "optimizer": optim_state},
    checkpoint_id=target_checkpoint,
)
set_state_dict(
    model,
    optimizer,
    model_state_dict=model_state,
    optim_state_dict=optim_state,
    options=options,
)
```

仓库里的 `scripts/gpu_source_save.py` 和 `scripts/gpu_target_load.py` 是这条
路径的 FSDP 示例。它们不是通用模型实现，用户应替换成自己的模型、优化器
和保存 key。

## 4. target root 怎么给训练任务

每个 target rank 通常有自己的本地 root：

```text
/targets/job42/rank0/.metadata
/targets/job42/rank0/__0_0.distcp
/targets/job42/rank0/w/rank0.bin
...
```

目标训练 Pod 必须挂载对应 rank 的 root，并把 `checkpoint_id` 指向该路径。
不要假设所有 rank 共享一个全局目录；local-disk 迁移的重点就是避免这个假设。

如果使用完整 DCP 汇总目录，则所有 rank 必须能访问同一个路径；这是共享
存储场景，不是 local-disk 的默认路径。

## 5. 常见错误

### `.metadata` 缺失

迁移可能还在 staging，或 target root 挂错了。先检查迁移状态和目录：

```bash
aevrelctl describe migration <name> -o json
```

### rank 的 `.distcp` 缺失

该 rank 没有完成 finalize/commit，或 target root 与 agent 实际写入路径不一致。
不要用其他 rank 的文件手工补齐。

### dtype/shape 不匹配

目标训练代码的模型结构与 source checkpoint 不一致，或 world size/DeviceMesh
不是迁移时写入的拓扑。回到 migration spec 和训练启动参数核对。

### allocator/OOM

全量 smoke 会分配完整 CPU tensor；大模型请用 rank-local smoke，并在真实
训练进程里使用 FSDP/DTensor load。

### source 被误改

如果 source 文件被改动，迁移校验应该失败；不要删除 source 或手工重写
journal。先保留现场并运行 support bundle。
