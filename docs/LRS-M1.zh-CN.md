# LRS M1：2+2 / F=1 语义证明

状态：已通过（2026-10-22）
范围：CPU、单进程模拟、无真实跨集群传输
代码：`src/shardferry/lrs/`
测试：`tests/unit/test_lrs_m1.py`
契约：[LRS-CONTRACT.zh-CN.md](LRS-CONTRACT.zh-CN.md)

## 1. M1 要证明什么

M1 只回答一个问题：

> 在 W=4、F=1、两个独立集群 A/B 的逻辑拓扑下，迁移一个 rank 的 shard
> 时，剩余 rank 能否继续同一个同步 global step，并在 cutover 后得到与
> 无迁移 baseline **bitwise 相同**的模型状态？

M1 证明的是**协议语义**，不是网络性能，也不是 GPU/NCCL 行为。

## 2. 拓扑与时间线

- W=4，4 个 shard，每个 rank 拥有一个 shard；
- 初始 4 个 rank 全在 A；
- 第一次迁移：rank 0，steps 4..10；
- 第二次迁移：rank 1，steps 12..18；
- 最终布局：rank 0/1 在 B，rank 2/3 在 A，即 2+2。

单次迁移的时间线：

1. step `S-1`：stable boundary，抓取 shard snapshot；
2. step `S` 开始前：source rank drain -> read-only；shard primary 原子转移给
   A 上的临时 owner（epoch+1）；target rank 被标记为 shadow；
3. step `S..C`：临时 owner 继续写该 shard；每个 step 的完整 shard 状态作为
   delta 流到 B；active ranks 吸收在途 rank 的数据分片，global batch 不变；
4. step `C` 结束后：target 重放 snapshot+delta，校验 digest；quorum 通过后
   把 primary 转给 target（epoch+1）；source 被 fence；
5. step `C+1`：target 以 B 上的 active rank 身份加入，global step 继续。

`steps_lost = 0`，`global_stop = 0`。迁移期间不会整任务停机；允许 per-shard
bubble，但不会跳 step。

## 3. 不变量在代码里怎么落地

| 不变量 | M1 实现 |
|---|---|
| I1 单一 owner | `OwnershipMap` + monotonic `epoch`；`assert_write` 拒绝非 primary/旧 epoch |
| I2 同步 step 连续 | `LRSSimulator.run` 按 step 循环；迁移不改变 step 序列；proof `steps_lost=0` |
| I3 global batch 不变 | `_assign_samples` 把在途 rank 的样本分给 active ranks；`_assert_assignment` 校验覆盖且不重复 |
| I4 coverage 每步校验 | `_run_step` 和 `_cutover` 调用 `assert_coverage` |
| I5 quorum | `_cutover` 用 `require_quorum`，M1 为全票 |
| I6 fence 先行 | source drain 后不可写；target 在 cutover 前只能是 shadow，`write_as_target` 被拒绝 |
| I7 source death | 不在 M1；M4 实现 |
| I8 无 checkpoint-restart | M1 没有该代码路径 |
| I9 proof | `MigrationProof` 记录 digest、step range、snapshot_step、temp_owner、epoch、coverage、quorum、timeline |
| I10 不双训练 | source `source_fenced=True`；shadow 不能写 primary |

## 4. 两种归约模式

M1 故意提供两种梯度归约模式，避免把“协议正确”和“浮点顺序”混在一起：

- `canonical`（默认）：按 sample id 排序后用 `math.fsum` 归约，baseline 与
  migration 的 theta/momentum/loss/batch **bitwise 相同**。它隔离协议语义。
- `rank`：每个 rank 用 float32 累加自己的样本梯度，再按 rank 顺序做 float32
  跨 rank 求和。baseline 与 migration 的样本分配不同，所以会有浮点差异；
  用容差验收。

M1 实测：

| 指标 | 结果 |
|---|---|
| float64 canonical theta | bitwise 相同 |
| float64 canonical momentum | bitwise 相同 |
| float64 canonical loss | bitwise 相同 |
| float64 canonical batch | bitwise 相同 |
| float32 rank `max abs Δtheta` | `2.98e-08` |
| float32 rank `max abs Δmomentum` | `1.19e-06` |
| float32 rank `max abs Δloss` | `3.14e-07` |

A1 验收要求 `max abs Δw <= 1e-6`，M1 的 float32 结果满足。

## 5. 怎么跑

```bash
cd /root/lines/oss/aicluster/shardferry-lrs
PYTHONPATH=src /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  -m pytest -q tests/unit/test_lrs_m1.py
PYTHONPATH=src /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  scripts/lrs_m1_demo.py
```

预期：`14 passed`；demo 输出 `M1 PASSED`，两段 proof 都是
`steps_lost=0 global_stop=0 digest_match=True`。

## 6. M1 不证明什么

- 不证明真实跨集群传输；M1 用 `InMemoryTransfer`；
- 不证明 NCCL/GPU；M1 是 CPU numpy；
- 不证明 source death takeover；M4；
- 不证明 TP/PP 重配置；M5；
- 不证明 F>1 并发；M2；
- delta 是完整 shard 状态，不是参数 diff；
- 不接真实 DCP/FSDP checkpoint，只是 toy linear regression；
- quorum 在 M1 是全票，没有网络分区；
- 不给性能数字。

这些不是“降级”，是 M1 的证明边界；每一条都在后续 milestone 里有对应验收。

## 7. 下一步

- M2：F>1，多 rank 同时在途，delta streams 并行；
- M3：跨集群传输（HTTP/QUIC）、断线重连、续传；
- M4：source death takeover；
- M5：TP/PP 重配置；
- M6：8+4 / 16+8 规模与故障矩阵。

## 8. 当前证据

- LRS 单测：`14 passed`；
- 全仓回归：`366 passed, 4 skipped`；
- demo：`M1 PASSED`。
- proof：`evidence/lrs/2026-10-22-m1/proof.json`。
