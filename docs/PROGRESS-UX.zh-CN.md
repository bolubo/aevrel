# 进度条与迁移状态 UX

状态：产品交互契约（2026-10-22）

## 1. 两种进度，不混用

### 全量迁移（static）

用户已经停止 source 或接受停机，显示可验证的字节进度：

```text
transfer [████████░░░░░░░░░░░░] 42% 1.2 GiB/2.8 GiB 14/32 nodes 86 MiB/s ETA 18.4s w/rank2
verify   [░░░░░░░░░░░░░░░░░░░░] checking 2.8 GiB
verified [████████████████████] 2.8 GiB
commit   [████████████████████] 32/32 nodes
```

- 本地 `shardferry migrate`：按真实 completed node/bytes 更新；
- Kubernetes `shardferryctl migrate --wait`：按 target rank 的 committed
  数量显示 rank 级进度；
- 不允许用定时器伪造百分比。

### LRS / live-rank-split

LRS 的进度是 rank/ownership 进度，不是伪造的字节进度：

```text
live [██████████░░░░░░░░░░] 50% settled=2/4 coverage=3/4 inFlight=1/2 quorum=3/3 stateComplete=false phase=following
```

- `inFlight` 的第二个数字来自用户配置的 `maxInFlight`；
- 进度条按 `settled = coverage - inFlight` 计算，避免在途 rank 被误算成已完成；
- `coverage` 表示当前已有 owner 的 rank 数，必须覆盖全部 rank 才算完成；
- `quorum` 必须达到多数；
- L4 没有 `stateComplete=true` 不能显示成功；
- `--ranks` / `describe` 提供每个 rank 的 owner/epoch/retry 细节。

`maxInFlight` 不是固定 1。用户可以通过 YAML 或
`shardferryctl migrate ... --max-in-flight N` 指定一次同时在途的 rank 数，
范围是 `1 <= N < source.worldSize`。

## 2. 输出规则

- TTY：同一行动画，使用 `\r`，宽度固定，完成后换行；
- 非 TTY/CI：只在新百分比分段或 phase 变化时打印，避免刷爆日志；
- `--json`：stdout 只输出一个 JSON object，进度完全不写入 stdout；
- `--no-progress`：关闭人类可读进度，适合自动化；
- 失败：打印当前阶段、已完成/总进度、错误原因和 `Next:`；
- 中断：保留 journal，提示重跑同一条命令续跑。

## 3. 阶段语义

```text
preparing -> transferring -> verifying -> committing -> target-validating -> succeeded
```

LRS 使用：

```text
pending -> preflight -> draining/snapshotting -> following -> cutover
        -> target-validating -> succeeded
```

任何阶段都不能把“文件存在”显示成“目标可训练”；target-ready 失败必须
显示为失败，而不是 100%。

## 4. 验收

- 进度数字来自真实执行事件，和最终 report 一致；
- 进程中断后重新运行能从 journal 续跑，进度从已完成节点继续；
- LRS 的 `inFlight`、`coverage`、`quorum`、`stateComplete` 与
  `describe --ranks` 一致；
- `--json` 下 stderr 不混入 stdout；
- 在 100Mbps/1Gbps/10Gbps 下 ETA 只作为估计，不作为成功条件。
