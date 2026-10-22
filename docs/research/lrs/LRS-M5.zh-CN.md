# LRS M5：TP=2 / PP=2 重配置

状态：CPU 语义验收通过（2026-10-22）；4-GPU NCCL 实机待租
范围：TP=2、PP=2 的 toy 模型；numpy CPU，4 个逻辑 rank
代码：`src/shardferry/lrs/tp_pp.py`
测试：`tests/unit/test_lrs_m5_tp_pp.py`
脚本：`scripts/lrs_m5_demo.py`
证据：`evidence/lrs/2026-10-22-m5/proof.json`

## 1. M5 要证明什么

M5 把之前的“每 rank 一个参数分片”扩展到 TP/PP 结构：

> TP=2、PP=2，4 个逻辑 rank；迁移 1 个 TP rank 和 1 个 PP stage 后，
> target 参与新的 TP collective / pipeline，最终 W1/W2 与 full-model
> baseline 数值一致。

## 2. Toy 模型

- W1：`d_in × hidden`，按 TP **列切分**；stage 0 的 rank 0/1 各持一半；
- W2：`hidden × d_out`，按 TP **行切分**；stage 1 的 rank 2/3 各持一半；
- stage 0：`h_partial = x @ W1_shard`，TP all-gather 得 `h`，ReLU；
- stage 1：`y_partial = h_slice @ W2_shard`，TP all-reduce 得 `y`；
- backward：stage 1 算 `dW2`、`da`，TP all-gather `da` 回 stage 0；
  stage 0 算 `dW1`；
- 两阶段顺序执行（不做 microbatch overlap），数值等价于 full model。

### TP rank 迁移

- 迁移 stage 0 的 rank 1；
- temp owner 是同 TP group 的 rank 0；
- snapshot + delta 到 B 上的 logical rank 1；
- cutover 后 rank 1 在 B active，`tp_epoch[0] += 1`；
- 下一次 stage 0 collective 的 owners 变为 `(0,1)`，rank 1 参与。

### PP stage 重映射

- 对 stage 1 的 logical rank 2/3 在 stable step 做 shadow；
- shadow owner 是 B standby 4/5；
- cutover 后 rank 2/3 DEAD，4/5 ACTIVE B；
- `tp_epoch[1] += 1`、`pp_epoch[1] += 1`；
- stage 1 collective owners 变为 `(4,5)`，pipeline 的 `to_owners` 也变为
  `(4,5)`。

## 3. 验收结果

### TP rank 迁移

- `w1_max_abs_delta = 0.0`；
- `w2_max_abs_delta = 0.0`；
- loss 与 baseline 一致；
- rank 1：`B/active`，`source_fenced`；
- proof：`tp_epoch=1`、`pp_epoch=0`、
  `collective_owners_after=(0,1)`、
  `steps_lost=0`、`global_stop=0`；
- post-cutover stage 0 collective owners 全为 `(0,1)`。

### PP stage 重映射

- `w1_max_abs_delta = 0.0`；
- `w2_max_abs_delta = 0.0`；
- loss 与 baseline 一致；
- proof：`old_owners=(2,3)`、`new_owners=(4,5)`、
  `tp_epoch=1`、`pp_epoch=1`；
- rank 2/3 DEAD；rank 4/5 ACTIVE B；
- post-cutover stage 1 collective owners 全为 `(4,5)`；
- pipeline `to_owners` 全为 `(4,5)`。

### 失败场景

- TP collective 缺一个 part / owner 不 active → `LrsError`；
- PP shadow 停留在旧 step → `CoverageError`；
- TP 迁移的 peer 不 active → `LrsError`。

### 测试

- M5：`5 passed`；
- LRS 合计：`45 passed`；
- 全仓：`397 passed, 4 skipped`。

## 4. M5 不证明什么

- 不是真实 torch/NCCL/4 GPU；toy 是 numpy CPU；
- 没有真实 TP/PP collective 延迟、pipeline microbatch overlap；
- 不接真实 DCP/FSDP，不碰 GPU 显存；
- TP rank 迁移用 `InMemoryTransfer`，不是 M3 的 HTTP；
- PP stage 重映射是 stable-step 冷 shadow，不是 live stage 迁移；
- 4-GPU 实机验收尚未做，需要租 4 卡。

## 5. 单卡 GPU 与 4 卡计划

单卡不足以跑 TP=2/PP=2 的 4 个 rank：

- 单卡用于 CUDA/torch smoke，确认驱动和 torch CUDA 可用；
- 2026-10-22 单卡 smoke 已通过：
  - `.venvs/lrs-gpu`：torch `2.14.1+cu130`、CUDA 13.0、
    RTX 5070 Ti Laptop 12GB、numpy 2.5.4；
  - full grad diff `<= 8.9e-16`，TP/PP shard diff `<= 2.8e-17`；
  - 证据：`evidence/lrs/2026-10-22-gpu/smoke.log`；
- 真正的 TP/PP NCCL 验证需要 4 卡；
- 建议一次租 4 卡窗口，跑：
  1. TP=2/PP=2 的 full baseline；
  2. 迁移 1 个 TP rank，比较 W1/W2 与 collective 参与；
  3. 重映射 1 个 PP stage，比较 W1/W2 与 pipeline 参与；
  4. 记录 step time、collective 时间和 target join 时间。

## 6. 下一步

M5 的 CPU 语义层已经闭环；下一步是 4-GPU NCCL 实机验证，再进入 M6
规模与故障矩阵。
