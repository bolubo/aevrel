# LRS RANK State Closure

状态：LRS toy 已接入并通过（2026-10-22）
范围：每 rank 独立 sampler/RNG；真实 target-ready L4 尚未接
代码：`src/shardferry/lrs/sampler.py`、`state.py`、`sim.py`；
      `src/shardferry/state/`（来自 resume-closure）
测试：`tests/unit/test_lrs_rank_state.py`
脚本：`scripts/lrs_rank_state_demo.py`
证据：`evidence/lrs/2026-10-22-rank/proof.json`

## 1. 深问题

之前的 LRS 只搬模型 shard（theta/momentum），没有搬 **rank-local 非 tensor
state**：

- 每 rank 的 RNG；
- dataloader/sampler 位置；
- scheduler/optimizer 非 tensor 元数据。

PyTorch DCP 会把同 key 的非 tensor 值当 replicated，导致不同 rank 的 RNG
被合并；target-ready 只到 L3，不保证后续训练轨迹一致。

## 2. 这次做了什么

- 从 resume-closure 引入 rank-local state provider（manifest/bundle/
  `NumpyRandomProvider`）；
- 每个 rank 一个 `RankSampler`，只从自己的 disjoint data shard 采样；
- 每步所有 rank（包括 in-flight 的逻辑 rank）都推进 sampler；
- `ShardBlob` 增加 `aux`（RNG state bytes），digest 包含 aux；
- snapshot/delta 带 aux；
- cutover 从 target blob 的 aux 重新构造 target sampler；
- takeover 的 shadow 也带 aux，B owner 激活时恢复 sampler；
- HTTP blob 格式增加 aux 长度字段，server 解析/校验 aux；
- aux 不匹配时 cutover/ takeover 直接 `DigestMismatchError`，不 commit。

## 3. 验收结果

- RANK 专项 4 tests：
  - 迁移后 theta/momentum/loss/batch bitwise；
  - 迁移后 rank1 的 sampler 下一个 draw 与 baseline 相同；
  - rank0/rank2 的 RNG 不受 rank1 迁移影响；
  - RNG 在 blob digest 中，aux 不同会 mismatch；
  - target RNG 错时 cutover fail closed，无 proof；
- M1–M6 全部仍然通过；
- 全仓：`449 passed, 4 skipped`；
- resume-closure 的 10 条 state 测试在 CUDA venv 下 `10 passed`
  （包括 CUDA RNG）。

## 4. 还没做

- target-load L4 已接（resume-closure）：`stateComplete`、restore 顺序、
  `verify_restored`、target job `require_state` 已有；
- 但真实 DCP/FSDP 训练循环的 N-step RNG/dataloader 轨迹证明还没做；
- A2 layout 一致性已有 1 test；M5 仍 hardcode，完整 adapter 未做；
- Go controller、NCCL/4 卡/2 节点仍未做；
- 任意 dataloader 内部 generator 不会自动捕获，需要显式 provider；

## 5. 下一步

1. 用真实 DCP/FSDP 小模型 + 显式 sampler provider 跑 N step，
   比较下一 batch/RNG；
2. 接 Go controller 的 `stateMode`；
3. 再做 NCCL communicator rebuild / rank handoff。

## 6. A3.1/A3.2 整合（2026-10-22）

- `TrainingStateProvider` 把 A3.1 的 model/Adam/RNG/generator/scaler/extra
  接进同一个 state bundle；
- `RankSampler` 放在 `TrainingState.extra["rank_sampler"]`；
- target restore 后 `verify_restored` 走语义比较（tensor/dict/RNG/extra）；
- integration test：uninterrupted 5 step vs 3 step capture + restore + 2 step；
  params、loss、下一 sampler batch 全部一致；
- LRS 全仓：`449 passed / 4 skipped`。

仍未接：真实 DCP/FSDP target job 的 N-step 轨迹、A2 VPS↔M5、
Go controller、NCCL/4 卡/2 节点。
