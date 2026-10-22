# LRS A3.1/A3.2：真实训练状态闭包

状态：A3.1 + A3.2 通过（2026-10-22）
并行会话已提供 `TrainingStateProvider`/bundle；本文的 `state_closure`
扩展是独立 prototype，A4 使用它，合并前先对齐。
代码：`src/shardferry/lrs/state_closure.py`
测试：`tests/unit/test_lrs_state_closure.py`

## 1. 做了什么

`TrainingState` 捕获/恢复：

- model `state_dict`（深拷贝，保证捕获后继续训练不会污染快照）；
- optimizer `state_dict`（Adam `step`/`exp_avg`/`exp_avg_sq`）；
- Python / NumPy / Torch CPU / Torch CUDA RNG；
- `torch.Generator` state；
- 可选 AMP `GradScaler`；
- `step` 和任意 JSON-safe `extra`（grad accumulation 等）。

序列化：

- `to_bytes()` 用 `torch.save`；
- `from_bytes()` 用 `torch.load(..., map_location="cpu", weights_only=False)`；
- `restore_state()` 恢复 model/optimizer/scaler/RNG/generator。

## 2. 吸收自哪里

- DynaTrain `TrainingState`：model/optimizer 生命周期、状态迁移；
- DynaTrain `DataloaderState`：dataset/iterator/consumed samples 的思路（下一版）；
- ElasWave per-step snapshot / RNG resharding：下一步扩展。

## 3. 验收

- 不中断 5 步 vs 3 步 + capture/serialize/restore + 2 步：
  - 最终参数 `allclose(atol=1e-6)`；
  - loss 序列 `allclose(atol=1e-6)`；
- capture 是快照：捕获后继续训练，旧快照参数不变；
- Python/NumPy/Torch/generator 状态 roundtrip 后随机序列一致；
- Adam state roundtrip 后 `step`/moments 完全一致；
- 4 个测试通过。

## 4. A3.2 补充

- `state_closure.py` 扩展：`consumed_samples`、`micro_batch_size`、
  `num_microbatches`、`grad_accum_steps`、`data_loader`、`rank_sampler`；
- 直接复用 RANK 会话的 `RankSampler.capture/from_state`，把 rank-local
  sampler 状态放进 `TrainingState`；
- `rng_reshard.py`：`RNGStreamKey` 支持 `data`/`layer`/`microbatch`；
  `RNGStreamRegistry.reshard(mapping)` 把 RNG 状态从旧 key 搬到新 key；
- `snapshot_ring.py`：capacity/eviction/latest + `NeighborBackup`；
- `memory.py`：`offload_model_and_optimizer` /
  `onload_model_and_optimizer`，model/optimizer state 在 host/device 间移动；
- 测试：A3.1 4 + A3.2 6 = 10 passed；全仓 `447 passed, 4 skipped`。
- demo：`scripts/lrs_a3_demo.py`；证据
  `evidence/lrs/2026-10-22-a3/proof.json`。

## 5. 还没做

- DCP/FSDP/DTensor sharded state；
- 真实 dataloader iterator 的自动捕获（当前是显式 `data_loader` 字段）；
- target-ready L4 的恢复顺序与 `stateComplete`；
- 真实 GPU 上的 handoff pause 测量。

这些是 A3.3 / A4 的内容。
