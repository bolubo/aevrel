# LRS 全局整合

状态：A3.2 / RANK / L4 已接；A2 VPS、真实 DCP/FSDP、Go controller、NCCL 待接
日期：2026-10-22

## 1. 分层

| 层 | 内容 | 状态 |
|---|---|---|
| A1 | prior art：NCCL grow/shrink、torchcomms、DynaTrain/ETC/ElasWave | 文档 |
| A2 | VPS/layout：任意 TP/PP 布局与 reshard plan | 27 tests + 1 A2→M5 一致性；M5 仍 hardcode |
| A3.1 | TrainingState：model/Adam/RNG/generator/scaler/extra | 4 tests |
| A3.2 | TrainingStateProvider ↔ state bundle ↔ RankSampler | 1 integration test |
| RANK | RankSampler/ShardBlob.aux 进入 LRS 迁移/takeover | 4 tests |
| L4 | target-load restore/verify + target job requireState | 7 tests |

## 2. 已闭环

- `RankSampler` 的 per-rank RNG 进入 `ShardBlob.aux`，LRS cutover/takeover
  恢复 target sampler；
- A2 `TensorLayout` 与 M5 TP/PP shard 的一致性（只读 A2 API）；
- A3.1 的完整训练状态通过 `TrainingStateProvider` 进入 state bundle；
- `RankSampler` 放在 `TrainingState.extra["rank_sampler"]`；
- target restore 后语义 verify；连续训练轨迹和下一 batch 与 uninterrupted
  一致；
- target-load 支持 `--state-root/--state-providers/--require-state-complete`；
- target job 注入 state env，`evaluate_target_reports(require_state=True)`
  强制每个 rank `stateComplete=true`。

## 3. 还没接

1. **A2 VPS ↔ M5**：已有一致性 test；M5 的 TP=2/PP=2 仍是 hardcode；
   完整 adapter 应使用 A2 的 `ParallelStrategy`/`ModelVPS`/`TransferPlan`
   生成 shard 布局与 reshard。
2. **真实 DCP/FSDP target job N-step**：target-load 已能 load DCP + restore
   state bundle；还要在真实训练 entrypoint 里跑 N step，比较下一 batch/RNG。
3. **Go controller/CRD `stateMode`**：Python target job 已有 require_state；
   Go controller 的 CRD/状态/模板还未接。
4. **NCCL rank handoff / 4 卡 / 2 节点**：NCCL communicator rebuild、
   RoCE/IB preflight、真 A/B。

## 4. Worktree 边界

- `shardferry-lrs`：A1/A2/A3.1/A3.2/RANK，LRS toy + state closure +
  `TrainingStateProvider`；全仓 `449 passed / 4 skipped`。
- `shardferry-resume-closure`：target-load/target-job L4、state bundle；
  L4 定向 `26 passed / 1 skipped`，全仓 `368 passed / 5 skipped`。
- 合并前不要声称“端到端已闭环”；两条线还要做一次 canonical merge。

## 5. 下一步顺序

1. A2 VPS 接 M5，去掉 TP/PP hardcode；
2. 真实 DCP/FSDP target job 跑 N step，接 `TrainingStateProvider`；
3. Go controller `stateMode`；
4. 4 卡/2 节点 NCCL + RoCE/IB；
5. 全部通过后再冻结 LRS v1。
