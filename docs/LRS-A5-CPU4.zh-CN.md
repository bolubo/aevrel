# LRS A5：CPU 4-rank 真实训练迁移前置

状态：通过（2026-10-22）
脚本：`scripts/lrs_cpu4_live.py`
测试：`tests/integration/test_lrs_cpu4_live.py`
证据：`evidence/lrs/2026-10-22-cpu4/live-split.json`

## 1. 这一层证明什么

用真实 gloo 进程组和真实 optimizer step，验证：

1. 4 个 rank 同步训练 20 步；
2. rank1 在 step 5 前退出；
3. 剩余 rank 0/2/3 组成 3-rank 世界，接管 rank1 的 4 条样本，
   global batch 仍为 16；
4. cutover 时抓取 rank1 的 sampler 状态和当前模型状态，经 TCP 交给
   独立 target 进程；
5. target 作为新 rank1 加入 4-rank 世界，继续 step 10..19；
6. 与不中断 baseline 比较参数、loss 和 sampler 下一批。

结果是：

| 指标 | 结果 |
|---|---:|
| 4→3→4 参数最大绝对误差 | `1.388e-17` |
| target 参数最大绝对误差 | `1.388e-17` |
| loss 最大绝对误差 | `0.0` |
| rank1 sampler 状态 | 完全一致 |
| 失败/丢 rank | 无 |

## 2. 真正的机制

- `RankSampler` 的 RNG bytes 属于 rank state，不是全局重建；
- phase1 中 rank1 的样本由 0/2/3 按 `2/1/1` 分担；
- 每个 rank 仍然执行真实的 `all_reduce(SUM)` 梯度聚合；
- target 进程先接收状态，再进入 `gen2` 进程组；
- model、optimizer、RNG、sampler 和 consumed samples 一起交接。

## 3. 必须说清楚的边界

这是 **CPU 前置**，不是“无停机 collective 热切换”的最终证明：

- `collective_transition = "destroy/reinit"`；
- 每个 phase 边界都会销毁并重建 gloo 进程组，所有 rank 有全局停顿；
- `global_stop = true`，不是 LRS contract 要求的 `global_stop = 0`；
- 模型是 DP 复制，不是 TP/PP/FSDP 分片；rank0 的模型副本可作为
  rank1 的状态来源，只因为所有 rank 参数在同步 step 后相同；
- 没有 NCCL、没有真实 GPU、没有跨机器/跨集群 WAN；
- 没有 fence、epoch、quorum、coverage 的真实运行时实现；
- 没有 source death takeover、没有 target 写失败/半状态场景。

因此，A5 的作用是排除“数据重分配、状态闭包、rank 替换、步数连续性”
这些前置风险；它不能被宣传成最终 LRS 爆点。

## 4. 运行

```bash
cd /root/lines/oss/aicluster/shardferry-lrs
PYTHONPATH=src \
  /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  scripts/lrs_cpu4_live.py
```

快速回归（8 步版本由测试自动设置）：

```bash
PYTHONPATH=src \
  /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  -m pytest -q tests/integration/test_lrs_cpu4_live.py
```

WSL 的 loopback 不可靠：gloo 默认选中 `127.0.0.1` 会连接超时。脚本会从
`/proc/net/route` 选择默认网卡；也可以显式设置：

```bash
GLOO_SOCKET_IFNAME=enP28624p0s0 PYTHONPATH=src \
  /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  scripts/lrs_cpu4_live.py
```

## 5. 下一道硬门槛

4-GPU 窗口必须把 `destroy/reinit` 换成真正的 NCCL communicator
reconfigure，并把验收改成：

- `steps_lost = 0`；
- `global_stop = 0`；
- rank1 在途期间其余 rank 继续同一个同步 step；
- target 加入后重新参与 all-reduce，loss/参数与 baseline 在容差内；
- 记录 reconfigure 时间、每步延迟、是否 hang、是否出现双训练。

在这之前，A5 只能算 CPU 4-rank 的前置闭环。
