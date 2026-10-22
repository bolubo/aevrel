# LRS M2：W=8 / F>1 并发迁移

状态：已通过（2026-10-22）
范围：CPU、单进程模拟、无真实跨集群传输
代码：`src/shardferry/lrs/`
测试：`tests/unit/test_lrs_m2.py`
前置：[LRS-M1.zh-CN.md](LRS-M1.zh-CN.md)

## 1. M2 要证明什么

M2 把 M1 的单 runtime 扩到多 runtime：

> W=8，F=1/2/3 个 rank 同时在途；剩余 `8-F` 个 rank 继续同一个同步
> global step；每个 stable step coverage 成立；没有死锁；所有 target
> 最终 join；step 不跳。

## 2. 并发设计

- `_runtimes` 从单个 runtime 改成 `dict[rank, runtime]`；
- `max_in_flight` 是显式参数，`run(..., max_in_flight=F)`；
- 同一 step 可以有多个 begin / cutover，用 list 处理；
- 每个 runtime 有独立的 snapshot、delta log、target state 和 inbox；
- `_write_shard` 把对应 shard 的 blob 送给对应 target，多个 stream 互不阻塞。
- proof 记录 cutover 时的 `in_flight_at_cutover`，F>1 不是口头声明，而是证据字段。

### temp owner 冲突

并发迁移最容易踩的坑是：rank A 正在做 rank B 的临时 owner，A 自己也要
迁走。M2 的处理是两层：

1. **调度期预留**：对每次 begin，把所有与 `[begin, cutover]` 重叠的
   migration rank 保留掉；temp owner 只从剩余 active A rank 中选，并按
   当前临时 owner 负载做 least-loaded 选择。
2. **运行期 fail closed**：如果动态调度仍让一个 temp owner drain，
   `_begin_migration` 直接报错，不会留下无 owner 的 shard。

## 3. 调度与布局

同一 step 并发：

| F | active | temp owners | final layout | theta digest |
|---|---|---|---|---|
| 1 | 7 | 1 | 7A + 1B | 相同 |
| 2 | 6 | 2, 3 | 6A + 2B | 相同 |
| 3 | 5 | 3, 4, 5 | 5A + 3B | 相同 |

错峰迁移（rank 0/1/2 分别在 step 4/6/8 开始，cutover 10/12/14）：

| step | in-flight | active |
|---|---|---|
| 4 | 0 | 7 |
| 6 | 0, 1 | 6 |
| 8 | 0, 1, 2 | 5 |
| 11 | 1, 2 | 6 |
| 13 | 2 | 7 |
| 15 | 无 | 8 |

## 4. 验收结果

- M2 单测：`12 passed`；
- LRS 合计：`26 passed`；
- 全仓回归：`378 passed, 4 skipped`；
- W=8、F=1/2/3：theta digest 均为 `fe11bb6fffcb84f5`，theta / momentum /
  loss / batch 全部 bitwise 相同；
- proof 的 `in_flight_at_cutover` 分别为 `[1]`、`[2, 1]`、`[3, 2, 1]`；
- fp32 F=3：`max abs Δtheta = 5.96e-08`、
  `Δmomentum = 4.17e-07`、`Δloss = 1.11e-07`；
- 失败场景：F 超限、temp owner 冲突、重复 rank、coverage 缺失都在
  commit 前 fail closed。

## 5. M2 不证明什么

- 不证明真实跨集群传输；仍是 `InMemoryTransfer`；
- 不证明 NCCL/GPU；CPU numpy；
- 不证明 source death takeover；M4；
- 不证明 TP/PP；M5；
- 不证明网络分区下的 quorum；M2 仍是全票；
- delta 仍是完整 shard 状态，不是参数 diff；
- 不给性能数字。

## 6. 下一步

M3：HTTP/QUIC 跨集群传输、断线重连、续传、target 落后 `K` 步的有界
lag。M3 仍可以先在 CPU + 两个 netns 上跑，不依赖 GPU。
