# LRS on-demand worker

状态：已接入 Go live 路径；当前 backend 是 CPU contract，GPU/真实训练
backend 待验证（2026-10-23）

## 角色

```text
Go controller
  --live-url / live.handoffEndpoint
        -> POST /v1/live/reconcile
        -> LRS worker (Python)
        -> phase / coverage / quorum / stateComplete / proofRef
        -> Go target-ready gate
```

Go controller 仍是 CR status 的唯一权威；worker 只返回观测，不修改
`CheckpointMigration`。worker 不存在或返回非法观测时，Go 会 fail closed，
不会静默回退成 static migration。

## 启动

```bash
aevrel-live-worker \
  --listen 0.0.0.0:9460 \
  --state-dir /var/lib/aevrel/live-worker \
  --token "$AEVREL_LIVE_TOKEN"
```

本地开发可以不设置 token；生产环境应设置 token，并让 controller 使用
同一个 Secret。Helm：

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set liveWorker.enabled=true \
  --set liveWorker.token.existingSecret=aevrel-live-token
```

chart 会自动给 controller 注入同 release 的 worker Service URL；也可以
设置 `controller.liveURL` 使用外部 worker。

## 协议

`POST /v1/live/reconcile` 接收 controller 生成的
`LiveRankSplitReconcile` 请求，返回：

```json
{
  "phase": "following",
  "inFlight": [0, 1],
  "coverage": [0, 1, 2, 3],
  "quorum": 3,
  "stateComplete": false,
  "stateRoot": "/state/step-100/rank{rank}",
  "proofRef": "",
  "message": "2 rank(s) in flight",
  "ranks": [
    {"rank": 0, "phase": "in_flight", "owner": 0, "epoch": 1}
  ]
}
```

最终成功会清空 `inFlight`、覆盖全部 logical ranks、返回 `proofRef`。如果
CR 配置了 `target.validation`，Go 会把 worker 的 `succeeded` 进一步压到
`target-validating`，直到真实 target Job 通过。

## 持久化与恢复

worker 在 `--state-dir` 下保存每个 migration generation 的 tick 和 proof
文件。controller/worker 重启后从 tick 继续，不会把同一 migration 又从
preflight 开始。proof 是内容文件，不塞进 CR status。

## 边界

- 当前 `cpu-contract` backend 用于验证 control-plane、phase、coverage、
  quorum、proof 和重启恢复；
- 它不声称 CUDA/NCCL collective 连续，也不替代 4 GPU/2 节点验收；
- 真实训练 backend 应保持同一 HTTP 契约，替换 worker 内部执行器，不放宽
  Go 的 target-ready 门禁；
- `stateComplete=true` 是 CPU contract 的闭包信号，不等同于真实模型/RNG/
  optimizer 已在 GPU 上验证。
