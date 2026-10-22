# Live Rank Split 用户流程

状态：Go 控制面 + CPU contract worker 已接；GPU/真实训练 backend 待验证
（2026-10-23）

## 1. 用户什么时候用它

适用：

- source 训练任务还在运行；
- 用户明确要把部分 rank 迁到 target；
- source/target 不共享 checkpoint 目录；
- 逻辑 world size `W` 不变；
- 用户能接受实验模式，不会把它当成当前生产 SLA。

不适用：

- source 已经停止：用 `mode: static` 的 `shardferryctl migrate`；
- 没有 controller `--live-url`；
- 没有启动 `shardferry-live-worker` 或外部 `POST /v1/live/reconcile`
  执行器；
- 没有 target-ready 验证能力却要求 L4 状态闭包。

## 2. 用户需要填什么

用户只写一份 `CheckpointMigration`，不需要写内部 journal：

```yaml
spec:
  mode: live-rank-split
  source:
    worldSize: 4
  target:
    worldSize: 4
    validation:
      version: step-100000
      steps: 10
  live:
    maxInFlight: 2
    stateMode: L4
    stateRoot: /state/step-100000/rank{rank}
    stateProviders:
      - python.random
      - numpy.random
      - torch.cpu.rng
      - training.state
    handoffEndpoint: http://lrs-handoff.live.svc:9460
    cutover:
      quorum: 3
      requireStateComplete: true
```

完整示例：
[`examples/live-rank-split/migration.yaml`](../examples/live-rank-split/migration.yaml)。

`maxInFlight` 不是固定为 1：它表示同时有几个 rank 在途，用户可以按网络、
target 容量和可接受的恢复风险设置；必须满足
`1 <= maxInFlight < source.worldSize`。示例用 `2`，表示一次迁移两个 rank。
也可以在 `preflight` / `plan` / `migrate` 上用 `--max-in-flight N` 覆盖 YAML。

## 3. 用户操作顺序

### 3.1 检查 spec 和发现 rank

```bash
shardferryctl preflight -f live-rank-split.yaml --json
```

用户应看到 `mode: live-rank-split`、source/target rank 数量，以及
source/target cluster 状态。这里不启动迁移。

### 3.2 预览 live 计划和前置条件

```bash
shardferryctl plan -f live-rank-split.yaml
```

输出必须清楚显示：

```text
Mode:           live-rank-split
Max in flight:  2
Prereq:         controller --live-url ...
Prereq:         executor must implement POST /v1/live/reconcile
Prereq:         target.validation is required for L4
Fallback:       none; switch to mode: static explicitly if live is unavailable
```

如果这里看到 `Fallback: none`，用户不能期待系统自动改走静态迁移。

### 3.3 先 dry-run，不创建资源

```bash
shardferryctl migrate -f live-rank-split.yaml --dry-run
```

确认生成的 CR 中 `mode`、`live`、`target.validation` 和 `stateRoot` 正确。

### 3.4 启动并观察

```bash
shardferryctl migrate -f live-rank-split.yaml --wait
```

worker 启动方式：

```bash
shardferry-live-worker \
  --listen 0.0.0.0:9460 \
  --state-dir /var/lib/shardferry/live-worker
```

Helm 默认关闭 worker；启用后 controller 会自动把它接到同 release 的
`live-worker` Service。当前 worker 是 CPU contract backend，负责
phase/coverage/quorum/proof 和持久化恢复，不等同于 GPU collective。

等待期间应出现类似：

```text
live [██████████░░░░░░░░░░]  50% settled=2/4 coverage=3/4 inFlight=1/2 quorum=3/3 stateComplete=false phase=following
```

这是 rank/ownership 级进度，不是伪造的字节动画；`inFlight` 的数量由
`maxInFlight` 决定，`coverage` 表示当前已有 owner 的 rank 覆盖。

### 3.5 排查和成功确认

```bash
shardferryctl describe migration live-rank-split-demo --ranks
```

成功必须同时满足：

- `inFlight` 为空；
- `coverage` 恰好覆盖 `0..W-1`；
- `quorum` 达到多数；
- L4 时 `stateComplete=true`；
- `TargetReady: ready=true`；
- proof 可读取。

只看到 `phase=succeeded` 但 target-ready 未通过，不是成功。

## 4. 失败时用户看到什么

| 用户看到 | 含义 | 下一步 |
|---|---|---|
| `LiveExecutorUnavailable` | controller 没有 `--live-url` 或执行器不可达 | 配置 `controller.liveURL`；不要偷偷切 static |
| `LiveObservationInvalid` | 执行器返回非法 phase、rank、coverage 或 quorum | 检查执行器输出；不要手工改 CR 绕过门禁 |
| `target-failed` | 文件可能已迁移，但 target 没有加载/跑够 N step | 看 `describe --ranks` 和 target Job 日志，修复后 retry |
| `failed` + retryable | 暂时网络/执行器错误 | 按 controller 的重试策略等待或 retry |
| `cancelled` | 用户取消 | 先确认 target 是否已 commit，再决定是否重跑 |

`describe` 和 `migrate --wait` 都应给出下一步命令或检查点；用户不需要
自己猜 `coverage`/`quorum` 的含义。

## 5. 绝对不能做

- 不要同时启动第二条 live migration；
- 不要手工修改 `status.live.coverage` 或 `stateComplete`；
- 不要把 `target-ready` 失败当成“文件已经在 target”；
- 不要在 live 不可用时静默改 `mode: static`；
- 不要在 4-GPU 验证前宣传 `global_stop=0` 或 `steps_lost=0` 已实现。

## 6. 当前边界

- Go controller 的契约、状态机、CLI dry-run 和 target-ready 门禁已实现；
- Python `/v1/live/reconcile` CPU contract worker 已实现，默认由 Helm
  `liveWorker.enabled` 控制；benchmark/真实训练 backend 未接入；
- CPU 4-rank 已证明状态/样本/step 连续，但使用 `destroy/reinit`，
  `global_stop=true`，不是最终 live collective；
- 4-rank NCCL reconfigure、source-death takeover 和跨机器 A/B 仍待真机。

因此，这份文档定义的是**用户将如何操作**，不是“现在可以生产使用”。
