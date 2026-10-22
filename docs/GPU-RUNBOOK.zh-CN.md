# GPU 两小时验收 Runbook

目标不是性能，而是用 4 张同实例 GPU 证明：

> 真实 FSDP/DCP 训练状态可以跨 world size 迁移，target 能恢复模型、优化器并
> 得到正确的前向输出。

## 0. 购买前

- 规格优先 **4×L20 48G**；有 4×A10 24G 也可；
- 按量付费，不要包年包月；
- 不要买 1 卡/2 卡来替代完整矩阵；
- 4×V100 只能做 fp32/fp16 兜底，不能测 bf16；
- 预留至少 100GB 系统盘，工作目录至少 20GB；
- 记录实例 ID、地域、可用区、创建时间和价格；
- 结束必须释放实例，次日核对账单。

## 1. 上机后先跑预检

```bash
cd /root/shardferry
python scripts/gpu_preflight.py \
  --min-gpus 4 \
  --min-disk-gb 20 \
  --work-dir /data/sf-gpu \
  --json
```

至少确认：

- 4 张 GPU；
- CUDA/torch/NCCL 可用；
- `torchrun` 存在；
- `shardferry` 可导入；
- 磁盘空间足够；
- L20/A10 之外如果需要 bf16，预检必须显示 bf16 supported。

预检失败就停止，不要先跑迁移。

## 2. 公共变量

```bash
export PYTHONPATH="$PWD/src:$PYTHONPATH"
export SF=/data/sf-gpu
mkdir -p "$SF"
```

每个 case 单独目录，禁止复用：

```text
$SF/a-4to2-fp32/
$SF/b-4to3-fp32/
$SF/c-4to2-bf16/
$SF/d-2to4-fp32/   # 可选
```

## 3. Case A：4→2 fp32

### source save

```bash
mkdir -p "$SF/a-4to2-fp32"
torchrun --standalone --nproc_per_node=4 \
  scripts/gpu_source_save.py \
  --out "$SF/a-4to2-fp32/source.dcp" \
  --fixture "$SF/a-4to2-fp32/fixture.pt" \
  --dtype float32
```

### migrate

```bash
python -m shardferry migrate \
  --checkpoint "$SF/a-4to2-fp32/source.dcp" \
  --target-world 2 \
  --output "$SF/a-4to2-fp32/target.dcp"
```

### target load

```bash
torchrun --standalone --nproc_per_node=2 \
  scripts/gpu_target_load.py \
  --ckpt "$SF/a-4to2-fp32/target.dcp" \
  --fixture "$SF/a-4to2-fp32/fixture.pt" \
  --dtype float32 \
  --atol 1e-6
```

必须看到 target 所有 rank `output_ok=True optimizer_state_loaded=True`。

## 4. Case B：4→3 fp32

```bash
mkdir -p "$SF/b-4to3-fp32"
torchrun --standalone --nproc_per_node=4 \
  scripts/gpu_source_save.py \
  --out "$SF/b-4to3-fp32/source.dcp" \
  --fixture "$SF/b-4to3-fp32/fixture.pt" \
  --dtype float32

python -m shardferry migrate \
  --checkpoint "$SF/b-4to3-fp32/source.dcp" \
  --target-world 3 \
  --output "$SF/b-4to3-fp32/target.dcp"

torchrun --standalone --nproc_per_node=3 \
  scripts/gpu_target_load.py \
  --ckpt "$SF/b-4to3-fp32/target.dcp" \
  --fixture "$SF/b-4to3-fp32/fixture.pt" \
  --dtype float32 \
  --atol 1e-6
```

## 5. Case C：4→2 bf16

```bash
mkdir -p "$SF/c-4to2-bf16"
torchrun --standalone --nproc_per_node=4 \
  scripts/gpu_source_save.py \
  --out "$SF/c-4to2-bf16/source.dcp" \
  --fixture "$SF/c-4to2-bf16/fixture.pt" \
  --dtype bfloat16

python -m shardferry migrate \
  --checkpoint "$SF/c-4to2-bf16/source.dcp" \
  --target-world 2 \
  --output "$SF/c-4to2-bf16/target.dcp"

torchrun --standalone --nproc_per_node=2 \
  scripts/gpu_target_load.py \
  --ckpt "$SF/c-4to2-bf16/target.dcp" \
  --fixture "$SF/c-4to2-bf16/fixture.pt" \
  --dtype bfloat16 \
  --atol 1e-2
```

bf16 不要求 `torch.equal`，但必须记录 tolerance；不要静默放宽。

## 6. 可选 Case D：2→4 fp32

只有 A/B/C 都完成后才做：

```bash
mkdir -p "$SF/d-2to4-fp32"
torchrun --standalone --nproc_per_node=2 \
  scripts/gpu_source_save.py \
  --out "$SF/d-2to4-fp32/source.dcp" \
  --fixture "$SF/d-2to4-fp32/fixture.pt" \
  --dtype float32

python -m shardferry migrate \
  --checkpoint "$SF/d-2to4-fp32/source.dcp" \
  --target-world 4 \
  --output "$SF/d-2to4-fp32/target.dcp"

torchrun --standalone --nproc_per_node=4 \
  scripts/gpu_target_load.py \
  --ckpt "$SF/d-2to4-fp32/target.dcp" \
  --fixture "$SF/d-2to4-fp32/fixture.pt" \
  --dtype float32 \
  --atol 1e-6
```

## 7. source 不变检查

每个 case 在 source save 后和 migrate 后分别计算：

```bash
find "$SF/a-4to2-fp32/source.dcp" -type f -print0 \
  | sort -z | xargs -0 sha256sum | sha256sum
```

两次摘要必须一致。B/C/D 同理。

## 8. 两小时时间盒

| 时间 | 动作 |
|---|---|
| 0–15 分钟 | 预检、环境版本、磁盘检查 |
| 15–55 分钟 | Case A 4→2 fp32 |
| 55–85 分钟 | Case B 4→3 fp32 |
| 85–110 分钟 | Case C bf16 |
| 110–120 分钟 | 证据整理；有余力再做 D |

任何一步失败：保留该 case 目录和日志，停止当前 case；不要在同一个目录
反复重试覆盖证据。

## 9. 结束

- 记录所有 case 的 stdout/stderr、GPU 型号、CUDA/torch/NCCL 版本；
- 记录 source/target 大小和耗时；
- 释放云实例；
- 次日核对账单；
- 把已验证/未验证项写回任务卡，不把 `脚本存在` 当成 `已验证`。
