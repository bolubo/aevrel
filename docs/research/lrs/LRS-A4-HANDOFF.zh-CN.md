# LRS A4：双进程训练状态 handoff

状态：CPU + 单卡 CUDA 通过（2026-10-22）
代码：`src/shardferry/lrs/handoff.py`、`state_closure.py`、`memory.py`
测试：`tests/unit/test_lrs_handoff.py`
脚本：`scripts/lrs_a4_demo.py`
证据：`evidence/lrs/2026-10-22-a4/{cpu,cuda}.json`

## 1. A4 要证明什么

两个独立进程之间，把完整训练状态从 source 交给 target：

> source 训练 3 步，抓取 model/Adam/RNG/generator/RankSampler/
> consumed samples/data loader，序列化后通过 TCP 发给 target；
> source 退出并释放 GPU；target 加载状态、接管 GPU、继续训练 2 步；
> 与不中断 baseline 的 5 步轨迹完全一致。

## 2. 设计

- `handoff.py`：length-prefixed TCP；`HandoffServer` / `send_state`；
  最大 8 GiB payload；接收后 ack；只传 `TrainingState.to_bytes()`。
- source：baseline 5 步；handoff model 3 步；
  `capture_state(..., rank_sampler=sampler.capture())`；发送后退出。
- target：先起 server；收到状态后等 `source_done`；source 退出后创建
  GPU model，`restore_state()`，`onload_model_and_optimizer()`，继续 2 步。
- 状态闭包：model/Adam/Python/NumPy/Torch/CUDA RNG/generator；
  `RankSampler` bytes；`consumed_samples`、`micro_batch_size`、
  `num_microbatches`、`grad_accum_steps`、`data_loader`。

## 3. 验收

| 设备 | param max delta | loss max delta | handoff pause |
|---|---|---|---|
| CPU | `0.0` | `0.0` | `0.807s` |
| CUDA（RTX 5070 Ti Laptop 12GB） | `0.0` | `0.0` | `1.148s` |

- baseline 5 步 vs source 3 + target 2；
- source 在 target 创建 GPU model 前退出；
- target loss 与 baseline 后 2 步一致；
- RANK sampler 的下一 draw 与 source 一致；
- 全仓：`449 passed, 4 skipped`。

## 4. 这是什么级别的 live

A4 证明的是 **state / step / GPU ownership** 的连续性，不是 NCCL
collective 的 live 重配：

- source 第 3 步后停住并退出，不是“边训练边交 rank”；
- target 是单 rank，没有 TP/PP、没有 communicator rebuild；
- handoff pause 包含 source 退出、target CUDA context 建立和状态加载；
- 传输是本地 TCP，不是 M3 的 HTTP/QUIC，也不是跨集群 WAN；
- 没有接真实 DCP/FSDP/DTensor。

这些边界必须保留，不能把 A4 当成最终 LRS demo。

## 5. 下一步

1. 对齐并行会话的 `TrainingStateProvider`/bundle，避免两套 state closure；
2. target-ready L4：`stateComplete`、恢复顺序
   model → optimizer → scheduler → RNG → dataloader；
3. 真实 DCP/FSDP 小模型 + 显式 sampler provider；
4. 4 卡 NCCL + torchcomms reconfigure，把 A4 的 state handoff 放到 TP/PP；
5. 两台机器 / 无共享存储的跨集群版本。
