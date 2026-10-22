# shardferry RTO 性能契约

状态：P1 基线  
日期：2026-10-22  
定位：单 source / 单 target 的 checkpoint warm standby；多集群自动放置暂不做

## 1. 产品定义

shardferry 不是“故障后开始搬 checkpoint”的工具。

它持续维护一个 target shadow checkpoint。故障发生时，系统只需要：

1. 选择最近完整版本；
2. 如果有 source，补最后一个 delta；
3. 校验；
4. fenced promote；
5. 启动/恢复 target 训练。

因此卖点是 **RTO/RPO**，不是“支持迁移”。

## 2. 指标定义

- **RPO**：目标最多丢失的已完成 checkpoint 版本数。P1 必须明确为一个
  checkpoint 间隔。
- **shadow lag**：target 最新完整 shadow 落后 source 完整版本的版本数。
- **cutover pause**：source 暂停到 target current 切换完成的墙钟时间。
- **RTO**：故障触发到 target job ready 的总时间。

P1 benchmark 先测：

```text
sync wall
+ cutover pause
+ target ready
```

不把 plan-only 时间当作 RTO。

`CutoverSession` 已提供 `target_ready` 回调；回调在 promote 之后执行，失败
会 rollback/clear current 并 resume source。benchmark 的 cutover 记录
`target_ready_seconds`。`shardferry.target_probe` 可作为第一版 target-ready
命令，校验 current、manifest root、CAS 和 shadow；GPU/DCP load hook 继续
在其外层包装。`/v1/status.last_cutover` 保存最近一次 RTO 两项耗时。

## 3. 数据放大定义

- `transfer_amplification = bytes_missing / changed_bytes`
- `target_storage_amplification = target_canonical_bytes / source_version_bytes`
- `read_amplification = source_bytes_read / source_version_bytes`
- `write_amplification = target_bytes_written / changed_bytes`

P1 目标：

- target 侧只保留一份 canonical CAS；
- shadow 用 hardlink/reflink；
- 不产生 provider cache；
- 不产生版本级全量副本；
- 后续版本只支付 delta。

## 4. P1 验收门槛

硬件口径：本机 Linux/WSL，单进程，源数据随机；比较时保持 world size、
chunk 策略和文件系统一致。

### 128MiB x 3

- full CDP wall <= `5s`
- cutover pause <= `0.2s`
- max RSS <= `100MiB`
- shadow lag after each sync == `0`
- target storage amplification <= `1.2x`

### 1GiB x 3

- full CDP wall <= `30s`
- cutover pause <= `1s`
- max RSS <= `150MiB`
- shadow lag after each sync == `0`
- target storage amplification <= `1.2x`

### 10GiB x 3

- full CDP wall <= `300s`
- cutover pause <= `5s`
- max RSS <= `500MiB`
- shadow lag after each sync == `0`
- target storage amplification <= `1.2x`

### plan-only

- 1GiB plan <= `1s`
- 10GiB plan <= `1s`

plan-only 只证明 planner 不是瓶颈，不等于数据面完成。

## 5. 正确性门槛

每个测试版本必须满足：

- 只接受完成 marker 的版本；
- manifest root 一致；
- target 文件 bitwise 一致；
- promote 只对应一个 `version_id + manifest_root`；
- source 不改写、不删除；
- cutover 失败可 rollback/resume；
- fencing 拒绝旧 epoch/旧 controller；
- controller/agent 在 pause、final delta、promote、rollback 的崩溃窗口可恢复。

## 6. 与社区方案比较

不能拿不同硬件、不同数据面、不同口径的数字直接比较。

正式对比必须至少记录：

- DCP save/load 默认路径；
- 全局 checkpoint 路径（先 upload 再 download）；
- shardferry plan-only；
- shardferry full CDP；
- 相同 source bytes、target bytes、world size、文件系统和 chunk 策略。

在完成同口径对比前，只能声明“结构性差异”，不能声明倍数。

## 7. 非目标

- P1 不做自动 target 选择；
- P1 不做自动故障检测；
- P1 不做 CRIU/GPU 进程热迁移；
- P1 不承诺源集群在复制完成前损坏的数据可恢复。

这些属于 P2。
