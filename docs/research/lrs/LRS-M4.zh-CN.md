# LRS M4：source death takeover

状态：已通过（2026-10-22）
范围：CPU、模拟器状态机；shadow 是 stable-step 冷拷贝，不是真实进程 kill
代码：`src/shardferry/lrs/protocol.py`、`sim.py`
测试：`tests/unit/test_lrs_m4_takeover.py`
脚本：`scripts/lrs_m4_demo.py`
证据：`evidence/lrs/2026-10-22-m4/proof.json`

## 1. M4 要证明什么

A 在迁移中途死亡时，B 只有在 **takeover quorum + coverage** 同时成立时
才能组成 degraded world；否则必须 fail closed，不能留下“看起来在跑、
实际 shard 残缺”的静默半状态。

## 2. 设计

### ShadowReplica

- 在最后一个 stable step，把 A 上尚未迁移的 shard 状态冷拷贝到 B standby；
- shadow 记录 `(shard_id, owner, blob)`；
- 它不改变 primary ownership，也不参与训练；
- takeover 时 shadow 必须正好在 `step-1`，且 digest 与 source 在该 step
  的 digest 一致。

### Takeover

- `Takeover(step, quorum=None)`；
- 只允许在 step 边界执行；
- M4 v1 不允许 in-flight migration 时 takeover，直接 `TakeoverError`；
- 每个 shard 必须满足以下之一：
  1. primary 是 B 上 active rank（已迁移）；
  2. 有 `step-1` 的 shadow，且 digest 与 source 一致；
- `b_ranks = sorted(set(owners))`；
- quorum 默认 `original_W // 2 + 1`，可显式覆盖；
- 通过后：所有 A rank 标记 DEAD，B owner 激活，ownership 强制切换并
  epoch+1，清空 shadow；
- 产出 `TakeoverProof`。

### 证明字段

`TakeoverProof` 记录：

- `step`、`degraded_world_size`；
- `b_ranks`、`coverage`；
- `quorum_required`、`quorum_votes`；
- `migrated_shards`、`shadow_shards`；
- 每个 shard 在 `step-1` 的 digest；
- `steps_lost=0`、`global_stop=0`。

## 3. 验收结果

### 正例 W'=8

- W=8；
- rank 0..5 在 steps 4..10 迁移到 B；
- rank 6/7 在 step 11 用 shadow 复制到 B standby 8/9；
- step 12 杀 A；
- 结果：theta/momentum/loss/batch 与 baseline **bitwise 相同**；
- proof：`degraded_world_size=8`、`b_ranks=[0..5,8,9]`、
  `coverage=0..7`、`migrated_shards=0..5`、`shadow_shards=[6,7]`、
  `quorum=8/5`；
- active `{0..5,8,9}`；A 6/7 DEAD；shard 6/7 primary 8/9。

### 正例 W'=6

- shadow 6/7 复制到已有 B rank 0/1；
- step 12 杀 A；
- 结果：bitwise；
- proof：`degraded_world_size=6`、`b_ranks=0..5`、
  `coverage=0..7`、`quorum=6/5`；
- active `{0..5}`；shard 6/7 primary 0/1。

### 负例

| 场景 | 结果 |
|---|---|
| 无 shadow / coverage 缺失 | `CoverageError`，无 proof，A 6/7 仍 active |
| quorum=9 > B ranks | `QuorumError`，无 proof |
| shadow 停留在 step 10，takeover 需要 step 11 | `CoverageError` |
| takeover 时有 in-flight migration | `TakeoverError` |
| migration cutover >= takeover step | `LrsError` |

### 测试

- M4：`8 passed`；
- LRS 合计：`40 passed`；
- 全仓：`392 passed, 4 skipped`。

## 4. M4 不证明什么

- 不是真实进程 kill + HTTP 的组合；M4 是模拟器状态机，M3 已单独验证
  HTTP 传输；真实 process kill 放到后续集成/M6；
- shadow 是冷拷贝，不是 live delta；没有带宽/性能结论；
- 不接真实 DCP/FSDP、不碰 NCCL/GPU、没有 TP/PP；
- 不证明时钟偏移、controller 重启、磁盘满、OOM；
- 不支持 in-flight migration 的 takeover；M4 v1 直接 fail closed；
- takeover 在 step 边界执行，不模拟 step 执行中 A 死亡。

## 5. Windows

- M4 的 simulator、测试、demo 都是纯 Python，Windows 原生可跑；
- M4 不需要 netns；M3 的 netns 脚本是 Linux-only；
- 单卡 GPU 在 M4 用不上。

## 6. 下一步

M5：TP/PP 重配置。

- 4 GPU，`TP=2/PP=2`；
- 迁移 1 个 TP rank 和 1 个 PP stage；
- target 参与 collective，数值与 baseline 一致；
- 单卡先做 CUDA/collective smoke，多卡再租 4 卡跑真 NCCL。
