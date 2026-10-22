# LRS A2：VPS-lite 布局代数与 model plan

状态：A2.1 通过（2026-10-22）
代码：`src/shardferry/lrs/layout.py`、`model_vps.py`
测试：`tests/unit/test_lrs_layout.py`、`test_lrs_model_vps.py`

## 1. 做了什么

### TensorLayout

- N-D tensor、任意 `Shard(dim)`、`Replicate`、`Partial(sum)`；
- 多个 mesh 轴 shard 同一维时按 mesh 顺序层级切分；
- 支持 uneven shard（10 分 4 份 → 3/3/2/2）；
- `plan_transfer` 输出 range 级 copy；
- `execute_plan` 用 numpy oracle 执行并校验；
- `byte_ranges` 生成 C-contiguous 字节区间。

### ModelVPS

- `ParallelStrategy(tp, pp, dp, ep)`；
- rank 顺序与 DynaTrain 一致：TP → EP → DP → PP；
- `ParamSpec`：name/shape/tp_dim/layer_id/stage；
- PP stage：embedding first、final/output last、transformer layer 按
  `num_layers/pp` 分；支持不均匀层数；
- `ModelVPS.plan` 把每个参数的 TP/PP/DP 计划组合成：
  - `send_info[src_global][dst_global]`；
  - `recv_info[dst_global][src_global]`；
  - `retain_info[rank]`；
  - 每个 copy 的 `retain` 表示同 global rank、无网络传输。

### FlatBufferLayout

- 参数按 buffer 顺序连续排布；
- 按 `bucket_size` 分桶；
- 每个 bucket 均分到 DP rank（支持余数）；
- `distribution(dp)` 输出每个 DP rank 的 `(param, offset, size)`；
- `plan_transfer(src_dp, dst_dp)` 输出 local offset 级 copy；
- `execute` 用 numpy flat buffer 验证；
- `retain` 表示同 DP rank（无网络），同 rank 内 local 重排也算 retain。

## 2. 吸收自哪里

- DynaTrain `VirtualParamSpace`/`VirtualParam`/`ReshardPlan`：
  参数级 VPS、TP/PP/DP 组合、`send_info/recv_info`、global rank 映射；
- DynaTrain `get_ddp_buffer_distribution`：
  flat DDP buffer / bucket / `intersect_range` / param range；
- ETC `Send/Recv/Refer`：
  `Refer` 对应我们的 `retain`；
- ETC `PP4→PP2` 例子：16 层 → 4 stage/2 stage。

没有复制 Megatron patch（约 7K 行），只移植数据模型和算法。

## 3. 验收

### DynaTrain 例子：TP2/PP2 → TP4/PP1

- 4 层 transformer + embedding/final/output；
- source `(tp=2,pp=2,dp=1)`，target `(tp=4,pp=1,dp=1)`；
- 目标 local shards 与 numpy oracle 完全一致；
- `network_bytes > 0`、`retain_bytes > 0`；
- `network + retain == target_bytes`；
- send/recv 对称。

### ETC 例子：PP4 → PP2

- 16 层，TP=1、DP=1；
- 目标：
  - `network_bytes = 12 × 8 × 8 = 768`；
  - `retain_bytes = 4 × 8 × 8 = 256`；
  - `total_bytes = 16 × 8 × 8 = 1024`；
- layers 0..3 retain；layers 4..7 从 source rank 1 发到 target rank 0；
- target stage 0 = layers 0..7，stage 1 = layers 8..15。

### Flat buffer：DP2 → DP4

- params `a=10,b=20,c=30`，bucket=32；
- 每个 source DP rank 30 个元素；每个 target DP rank 15；
- `execute` 从零初始化目标，再由 copy 填充，结果与 oracle 一致；
- no-op DP2→DP2：network=0，retain=60×8。

### 测试

- layout：12 passed；
- model_vps：7 passed；
- planning：8 passed；
- A2 合计：27 passed。

## 4. A2.2 补充

- `planning.py`：
  - ETC cost matrix：`CostMatrix.from_model_plan`；ETC PP4→PP2 的
    `costs=(256,256,512,512)/(512,512,256,256)`，配对 total=512，
    2 个 source 未配对；
  - Hungarian 矩形分配（覆盖较小侧）；
  - meta TP 推断：W1 `(4,4)/(4,2)` TP2→dim1；W2 `(4,1)/(2,1)`→dim0；
    LayerNorm→None；square ambiguous 抛错；
  - `coalesce_flat_copies` / `coalesce_model_copies`；
  - `xor_schedule`：0-1/2-3 在 round 1，0-2/1-3 在 round 2；
  - `promote_copies`：broadcast / gather / reduce / p2p；
  - interleaved FlatBuffer：rank j 拥有每层第 j 个 shard；DP2→DP4
    迁移是点对点，避免 contiguous 的 all-to-all。
- 测试：planning 8 passed；A2 合计 27 passed。
- demo：`scripts/lrs_a2_demo.py`；DynaTrain plan `network=1376`、
  `retain=800`、`total=2176`；interleaved `network=336`、`retain=144`、
  `total=480`；证据 `evidence/lrs/2026-10-22-a2/proof.json`。

## 5. 还没做

- DynaTrain `TransferManager` 三阶段执行器（pre/main/post）；
- EP/MoE 维度；
- 真实 Megatron/DCP/FSDP 适配。

这些是 A2.3/A4 的内容。
