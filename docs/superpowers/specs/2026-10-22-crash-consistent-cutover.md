# 尖刀 3：崩溃一致 commit / cutover 协议

状态：M1/M2/M3 已完成；CutoverService HTTP/CLI 联动完成  
日期：2026-10-22  
前置：尖刀 1 布局编译器、尖刀 2 零物化 DCP runtime

## 1. 一句话目标

在 source/target 生命周期、controller 重启、并发 controller 和 agent
异常下，保证 checkpoint **只有一个可验证的 current version**，且任何
promote/rollback 都不会留下半提交状态。

## 2. 现有基础

- `cdp.py`：version marker、内容寻址 ChunkStore、VersionManifest、
  delta follower、VersionTarget、`FenceStore`。
- `cutover.py`：pause -> final catch-up -> verify -> promote -> rollback，
  pause budget 和 recovery errors。
- 现有测试覆盖非最新版本拒绝、pause 失败、final delta 失败、promote 失败、
  rollback 失败和 stale fence。

## 3. 本刀清单

### M1：协议正确性

- [x] durable version marker 和 manifest root
- [x] content-addressed chunk store
- [x] delta-only catch-up
- [x] pause budget / resume
- [x] promote / rollback / clear current
- [x] monotonic fence epoch + stale owner rejection
- [x] target-root cross-process lock（本批）
- [x] concurrent fence acquire exactly-one-winner
- [x] crash injection at pause / final delta / promote / rollback（SIGKILL）

本批证据：`tests/unit/test_cdp.py` 的跨线程双 controller acquire 只有一个成功；
`FenceStore.acquire` 与 `VersionTarget.promote/rollback/clear_current` 共用
target-root lock。

### M2：controller / agent 集成

- [x] controller 生成每 run/retry 的 commit token 并写入 Task
- [x] CR/status 暴露 commitToken
- [x] CR/status 暴露 cutoverPhase（controller commit 生命周期投影）
- [x] agent task commit 绑定 commit token，而不是只绑定 task hash
- [x] target-root lock 包住 task create 和 commit，旧 token 进入 commit guard 前拒绝
- [x] duplicate task / duplicate commit 幂等（现有 task state + token 校验）
- [x] agent restart 后从 task + target journal 恢复 transfer/commit
- [x] kill -9 在 commit 第一条 target rename 后恢复并完成剩余 commit
- [x] CutoverService：认证 `status/follow/cutover/recover` HTTP API
- [x] `shardferry cutover-serve` CLI 入口
- [x] Go controller 的 `internal/cutoverclient`，真实读取 service `status`、
      `follow`、`cutover`、`recover`
- [x] controller `--cutover-url` / `--cutover-auto-promote` 联动；CR
      `status.cutoverPhase` 不再只是 commit 生命周期投影

### M3：可验证性

- [x] TLA+ 模型已落盘，并有 Python bounded equivalent model test
  （`spec/tla/CheckpointCutover.tla`、`tests/unit/test_cutover_model.py`）
- [x] TLC 2.19：455 states generated / 91 distinct, no invariant violation
- [x] 多进程故障注入：cutover/agent commit 的 kill -9
- [x] 目标目录半写、CAS 损坏、锁持有者死亡
- [x] 128-rank / 多版本 cutover 性能与 pause 实测

128 source → 64 target、3 版本、128MiB benchmark：sync `0.497s / 0.066s /
0.075s`，cutover pause `2.8ms`，max RSS `51.6MiB`。

## 4. 验收条件

### A1 Fencing

- 同一 epoch 不同 owner 并发 acquire，只有一个成功；
- 新 epoch 可以驱逐旧 owner，旧 token 在 promote/rollback 前被拒绝；
- 进程崩溃后锁自动释放，不会永久锁死 target。

### A2 Crash consistency

- pause 前失败：source resume，target current 不变；
- final delta 失败：source resume，target current 不变；
- promote 前失败：current 保持旧版本；
- promote 后失败：可 rollback 到旧版本；若 rollback 失败，必须
  返回 recovery error，不能报告成功；
- 重复 cutover / promote 幂等，不产生第二个 current pointer。

### A3 Compatibility

- 现有 `test_cdp.py`、`test_cutover.py`、`test_follower.py` 全绿；
- 不改变现有 manifest/chunk 的外部格式；
- 不破坏尖刀 1/2 的 DCP 和 agent 路径。

## 5. 非目标

- 不在本批重写 controller 的 commit state machine；
- 不把 CDP 和旧 shardferry `apply` 路径强行合并；
- 不承诺 Windows 上的 POSIX flock 语义；Windows 使用显式 fallback。
