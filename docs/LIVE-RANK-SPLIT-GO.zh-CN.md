# Live Rank Split：Go 产品面

状态：Go 控制面契约已实现；Python LRS 执行器待接入 `/v1/live/reconcile`
日期：2026-10-22

## 1. 用户入口

用户不需要启动新的 CRD 或新的 CLI。Live Rank Split 是
`CheckpointMigration` 的一个模式：

```yaml
spec:
  mode: live-rank-split
  live:
    maxInFlight: 1
    stateMode: L4
    stateRoot: /state/step-100000/rank{rank}
    stateProviders: [python.random, numpy.random, torch.cpu.rng, training.state]
    handoffEndpoint: http://lrs-handoff.live.svc:9460
    cutover:
      quorum: 3
      requireStateComplete: true
    rankPlan:
      tp: 2
      pp: 2
      ranks: [0, 1, 2, 3]
```

完整示例：
[`examples/live-rank-split/migration.yaml`](../examples/live-rank-split/migration.yaml)。

操作方式与其他迁移相同：

```bash
shardferryctl migrate -f examples/live-rank-split/migration.yaml --dry-run
shardferryctl migrate -f examples/live-rank-split/migration.yaml --wait
shardferryctl describe migration live-rank-split-demo --ranks
```

`describe` 会显示：

```text
Live: phase=following inFlight=[2] coverage=[0 1 2 3] quorum=3 stateComplete=false
```

## 2. 控制器配置

Controller 新增一个可选外部执行器：

```text
--live-url http://lrs-handoff.live.svc:9460
```

Token 默认复用 `SHARDFERRY_AGENT_TOKEN`，也可单独设置
`SHARDFERRY_LIVE_TOKEN`。mTLS 复用 `--agent-ca/--agent-cert/--agent-key`。

如果 CR 使用 `mode: live-rank-split` 但控制器未配置 `--live-url`，
迁移会以 `LiveExecutorUnavailable` 失败关闭，不会退回全量停机模式。

## 3. 执行器契约

Controller 对每个未完成迁移调用：

```text
POST <handoffEndpoint>/v1/live/reconcile
Authorization: Bearer <token>
Content-Type: application/json
```

请求是幂等的期望状态：

```json
{
  "apiVersion": "shardferry.io/v1",
  "kind": "LiveRankSplitReconcile",
  "migration": {
    "namespace": "default",
    "name": "live-rank-split-demo",
    "uid": "...",
    "generation": 1
  },
  "spec": { "...": "完整 CheckpointMigrationSpec" },
  "previousStatus": { "...": "上一次 Status.Live，首次为空" }
}
```

响应：

```json
{
  "phase": "following",
  "inFlight": [2],
  "coverage": [0, 1, 2, 3],
  "quorum": 3,
  "stateComplete": false,
  "stateRoot": "/state/step-100000/rank{rank}",
  "proofRef": "",
  "message": "rank 2 snapshot is following source",
  "ranks": [
    {"rank": 2, "phase": "in-flight", "owner": 2, "epoch": 7, "retries": 0}
  ]
}
```

允许的 `phase`：

```text
pending preflight draining snapshotting following cutover
target-validating succeeded failed cancelled
```

Controller 只负责观察和门禁，不把 CR 当作 LRS 的内部 journal。

## 4. 成功门禁

Live Rank Split 不能仅凭执行器返回 `succeeded` 就把迁移标记成功。Go
controller 还会强制检查：

1. `inFlight` 为空；
2. `coverage` 恰好覆盖 `0..worldSize-1`，无重复、无越界；
3. `quorum >= floor(worldSize/2)+1`；
4. L4 或显式 `requireStateComplete` 时，`stateComplete=true`；
5. 配置了 `target.validation` 时，必须再跑 target-ready Job；
6. target-ready 报告中每个 rank 的 `stateComplete=true`，否则以
   `StateIncomplete` 失败关闭。

第 5 条意味着执行器可以先返回 `target-validating`，由 Go controller 启动
target validation Job；也可以返回 `succeeded`，但 controller 会把它改写成
`target-validating`，直到 Job 证明完成。

## 5. 失败语义

- 执行器网络错误：状态保持 `transferring`，条件
  `LiveReady=False`，按固定间隔重试；不会误报成功。
- 执行器明确返回 `failed/cancelled`：迁移进入终态，不再自动重试。
- 返回非法 phase、越界 rank、重复 coverage、quorum 不足：立即
  `LiveObservationInvalid` 失败关闭。
- 未配置执行器：`LiveExecutorUnavailable` 失败关闭。
- 任何情况下都不回退为“停训练→写 checkpoint→重启”。全量停机是
  `mode: static`，不是 LRS 的 fallback。

## 6. 当前边界

- Go 侧契约、状态机、校验、CLI 示例和 target-ready L4 注入已完成；
- Python LRS 服务尚未实现 `/v1/live/reconcile`，因此当前不能声称端到端；
- 4-rank NCCL、source-death takeover、真实跨集群 A/B 仍需真机窗口；
- 该模式仍是实验能力，不进入 0.1.1 的生产 SLA。
