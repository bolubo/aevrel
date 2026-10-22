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

### 0.1 上机环境

云镜像应自带 NVIDIA driver 和 CUDA；不要手工替换驱动，除非预检明确要求。

```bash
cd /data/shardferry
python3 --version                 # Ubuntu 22.04 默认是 3.10
nvidia-smi                        # 必须看到 4 张卡
```

若没有 Python 3.11，从阿里云镜像编译独立解释器，不改系统 Python：

```bash
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev \
  libncursesw5-dev uuid-dev
curl -fL https://mirrors.aliyun.com/python-release/source/Python-3.11.13.tgz \
  -o /tmp/Python-3.11.13.tgz
tar -xzf /tmp/Python-3.11.13.tgz -C /tmp
cd /tmp/Python-3.11.13
./configure --prefix=/data/python311 --with-ensurepip=install
make -j"$(nproc)"
sudo make install
```

然后创建并激活项目环境：

```bash
cd /data/shardferry
/data/python311/bin/python3.11 -m venv .venv
. .venv/bin/activate

# 阿里云 ECS 优先用内网镜像；非 ECS 环境把 cloud.aliyuncs.com 换成
# mirrors.aliyun.com，并去掉 --trusted-host。
python -m pip install -U pip \
  --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
  --trusted-host mirrors.cloud.aliyuncs.com

# 镜像为 CUDA 12.8；不要装 CPU wheel。
python -m pip install "torch==2.11.0+cu128" \
  --find-links http://mirrors.cloud.aliyuncs.com/pytorch-wheels/cu128/ \
  --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
  --trusted-host mirrors.cloud.aliyuncs.com
python -m pip install -e .
```

确认：

```bash
python - <<'PY'
import torch
print(torch.__version__, torch.version.cuda, torch.cuda.device_count())
print(torch.cuda.is_available(), torch.distributed.is_nccl_available())
PY
```

如果这里没有 CUDA 或 NCCL，不要继续跑 case。

## 1. 上机后先跑预检

```bash
cd /data/shardferry
python scripts/gpu_preflight.py \
  --min-gpus 4 \
  --min-disk-gb 20 \
  --work-dir /data/sf-gpu \
  --require-bf16 \
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

## 10. 证据模板

```text
instance:
  id:
  region/zone:
  gpu:
  driver/cuda:
  torch/nccl:
  billing: pay-as-you-go
  started_at:
  released_at:

case:
  name:
  source_world / target_world:
  dtype / atol:
  source_digest_before:
  source_digest_after:
  migrate_exit:
  target_rank_results:
  target_load_ok:
  wall_time:
  peak_gpu_memory:
  notes:
```

## 11. 云资源清理

创建前先记录：实例名/ID、地域/可用区、计费模式、创建时间、小时价。

结束后逐项确认：

- ECS 实例已释放，不是“停止”；
- 系统盘/数据盘/快照已释放；
- 公网 IP/EIP 已释放；
- 负载均衡、NAS、对象存储等临时资源已删除；
- 次日账单没有新增用量；
- 把删除时间、资源 ID、账单核对结果写回任务卡。
