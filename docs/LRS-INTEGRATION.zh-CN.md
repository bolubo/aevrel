# LRS 全局整合

状态：A3.2 / RANK / L4 / Go 产品契约 / A5 CPU4 / CPU contract worker 已接；
A2 VPS、真实 DCP/FSDP、NCCL 4 卡待接
日期：2026-10-23

## 1. 分层

| 层 | 内容 | 状态 |
|---|---|---|
| A1 | prior art：NCCL grow/shrink、torchcomms、DynaTrain/ETC/ElasWave | 文档 |
| A2 | VPS/layout：任意 TP/PP 布局与 reshard plan | 27 tests + 1 A2→M5 一致性；M5 仍 hardcode |
| A3.1 | TrainingState：model/Adam/RNG/generator/scaler/extra | 4 tests |
| A3.2 | TrainingStateProvider ↔ state bundle ↔ RankSampler | 1 integration test |
| A4 | 双进程 state handoff（`handoff.py`，CPU/CUDA 0.0） | A4 会话完成；已合入 |
| A5 | CPU 4-rank 真实 gloo 4→3→4 + state handoff | 20-step proof；`global_stop=true`，非最终 live collective |
| RANK | RankSampler/ShardBlob.aux 进入 LRS 迁移/takeover | 4 tests |
| L4 | target-load restore/verify + target job requireState | 7 tests |
| GO/Python worker | CRD `mode=live-rank-split`、`/v1/live/reconcile`、CPU contract worker、fail-closed 门禁 | 已接；GPU/真实训练 backend 待验证 |

## 2. 已闭环

- `RankSampler` 的 per-rank RNG 进入 `ShardBlob.aux`，LRS cutover/takeover
  恢复 target sampler；
- A2 `TensorLayout` 与 M5 TP/PP shard 的一致性（只读 A2 API）；
- A3.1 的完整训练状态通过 `TrainingStateProvider` 进入 state bundle；
- A4 的双进程 state handoff（source 3 步 + 完整 state → target 2 步）
  已合入 integration；
- `RankSampler` 放在 `TrainingState.extra["rank_sampler"]`；
- target restore 后语义 verify；连续训练轨迹和下一 batch 与 uninterrupted
  一致；
- target-load 支持 `--state-root/--state-providers/--require-state-complete`；
- target job 注入 state env，`evaluate_target_reports(require_state=True)`
  强制每个 rank `stateComplete=true`。
- A5 CPU4：rank1 退出后由 0/2/3 接管样本，target 恢复 rank1 状态；
  `param_diff=1.388e-17`、`loss_diff=0`、sampler 一致。

## 3. 还没接

1. **A2 VPS ↔ M5**：已有一致性 test；M5 的 TP=2/PP=2 仍是 hardcode；
   完整 adapter 应使用 A2 的 `ParallelStrategy`/`ModelVPS`/`TransferPlan`
   生成 shard 布局与 reshard。
2. **真实 DCP/FSDP target job N-step**：target-load 已能 load DCP + restore
   state bundle；还要在真实训练 entrypoint 里跑 N step，比较下一 batch/RNG。
3. **Go controller + Python worker**：已接
   `mode=live-rank-split`、`/v1/live/reconcile`、CPU contract backend、
   coverage/quorum/stateComplete/target-ready 门禁和 durable tick/proof；
   见 [Live Rank Split Go 产品面](LIVE-RANK-SPLIT-GO.zh-CN.md)。
   剩余是真实训练/GPU backend，不应把 CPU contract 当成 GPU 证明。
4. **NCCL rank handoff / 4 卡 / 2 节点**：NCCL communicator rebuild、
   RoCE/IB preflight、真 A/B。

## 4. Worktree 边界

- `shardferry-lrs`：A1/A2/A3.1/A3.2/RANK/A5，LRS toy + state closure +
  `TrainingStateProvider` + CPU4/GPU 前置；全仓 `450 passed / 4 skipped`。
- `shardferry-resume-closure`：target-load/target-job L4、state bundle；
  L4 定向 `26 passed / 1 skipped`，全仓 `368 passed / 5 skipped`。
- `shardferry-integration`：canonical merge 已完成；
  `feature/lrs-integration` @ main `14a6909`；全仓 `465 passed / 5 skipped`；
  未 commit/push。
- 合并已完成，但仍不要声称“端到端已闭环”；A2 full adapter、真实
  DCP/FSDP N-step、Go、NCCL 未做。

## 5. 下一步顺序

1. A2 VPS 接 M5，去掉 TP/PP hardcode；
2. 真实 DCP/FSDP target job 跑 N step，接 `TrainingStateProvider`；
3. 把 CPU contract backend 替换/扩展成真实训练 worker；
4. 4 卡/2 节点 NCCL + RoCE/IB；
5. 全部通过后再冻结 LRS v1。
