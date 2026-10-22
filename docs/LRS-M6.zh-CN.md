# LRS M6：8+4 / 16+8 规模与故障矩阵

状态：CPU/HTTP 规模与已实现故障项通过（2026-10-22）
范围：W=12/24、F=2/4/8、HTTP 断链、8+4 takeover、fail-closed
代码：`src/shardferry/lrs/`
测试：`tests/unit/test_lrs_m6_scale.py`、`tests/integration/test_lrs_m6_scale_http.py`
脚本：`scripts/lrs_m6_demo.py`
证据：`evidence/lrs/2026-10-22-m6/proof.json`

## 1. M6 要证明什么

M6 检查 LRS 在更大 world size 和故障下是否仍然：

- 保持 step 连续、global batch 不变；
- 每个 stable step coverage 成立；
- 成功场景 `steps_lost=0`、`global_stop=0`；
- 失败场景在 commit 前 fail closed，不产生静默半状态。

## 2. 规模验收

| 场景 | 结果 |
|---|---|
| W=12，8+4，F=2 | bitwise；4 proofs；最终 8A+4B |
| W=12，8+4，F=4 | bitwise；4 proofs；最终 8A+4B |
| W=24，16+8，F=4 | bitwise；4 proofs；最终 20A+4B |
| W=24，16+8，F=8 | bitwise；8 proofs；最终 16A+8B |

所有规模场景：

- theta / momentum / loss / batch 与 baseline 完全一致；
- 每个 proof 的 `steps_lost=0`、`global_stop=0`；
- `in_flight_at_cutover` 按 `F..1` 递减；
- B 侧 rank 集合正好是迁移的 rank 前缀。

## 3. HTTP 规模 + 断链

W=12，F=4，steps 4..12：

- 目标：B 侧 4 个 target，lag `K=2`；
- `DroppingProxy` 丢前 3 个 POST；
- sink 重连并从 ack 续传；
- 结果：`ok=True`、`lag_max=2`、theta/momentum digest 与 baseline 一致；
- 最终布局 8A+4B；
- 4 个 proof 的 `in_flight_at_cutover=[4,3,2,1]`；
- 每个 target 的 `latest.json` step=12，digest 与 proof 一致。

## 4. 8+4 takeover

W=12：

- rank 0..7 在 steps 4..10 迁移到 B；
- rank 8..11 在 step 11 shadow 到 B standby 12..15；
- step 12 杀 A；
- 结果：theta/momentum/loss 与 baseline bitwise；
- proof：`degraded_world_size=12`、
  `b_ranks=0..7 + 12..15`、`coverage=0..11`、
  `migrated_shards=0..7`、`shadow_shards=8..11`、`quorum=12/7`；
- active 12 个 rank；旧 A 8..11 DEAD。

无 shadow 时：`CoverageError`，无 takeover proof，A 侧 rank 8/9 仍 active。

## 5. 故障矩阵

| 故障 | 覆盖情况 |
|---|---|
| 网络丢包/断链 | M3 + M6 HTTP scale，丢 3 次 POST 后恢复 |
| source death | M4 + M6 8+4 takeover |
| coverage 缺失 | M4 + M6 无 shadow fail closed |
| quorum 不足 | M4 |
| stale epoch | M1 + M6 W=12 |
| 重复投递 | M3 server 幂等 |
| digest/checksum 不一致 | M1 + M4 shadow mismatch |
| in-flight migration takeover | M4 fail closed |
| 磁盘满 | 未覆盖 |
| OOM | 未覆盖 |
| 时钟偏移 | 未覆盖 |
| controller 重启 | 未覆盖 |
| 真实进程 kill + HTTP 组合 | 未覆盖（M4 是状态机 takeover） |
| 4 GPU NCCL | 未覆盖（待租 4 卡） |

“未覆盖”不是通过；后续 M6.x 或 4-GPU 窗口补。

## 6. 测试

- M6：`8 passed`（4 个规模参数 + takeover + no coverage + stale epoch
  + HTTP scale）；
- RANK state：`4 passed`；
- LRS 合计：`57 passed`；
- 全仓：`425 passed, 4 skipped`。

## 7. 边界与下一步

M6 的 CPU/HTTP 语义层已经闭环，但还没有真实 4 GPU/NCCL、
真实进程 kill、磁盘/OOM/时钟/controller 重启。

下一步：

1. 4-GPU NCCL 窗口：TP=2/PP=2 实机，迁移 1 个 TP rank 和 1 个 PP stage；
2. 补 M6 未覆盖故障项；
3. 全部通过后再冻结 LRS v1 contract。
