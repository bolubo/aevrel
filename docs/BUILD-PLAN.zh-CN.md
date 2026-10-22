# shardferry 产品构建计划

状态：开发执行基线（2026-10-22）
目标：一次完成产品闭环，之后只做维护/安全/兼容修复

## 0. 完成定义

产品闭环：

```text
scan local checkpoint
  -> CheckpointRecord
  -> CheckpointReadiness
  -> RescueDecision
  -> RestoreDrill / MobilityTransaction
  -> Proof
  -> readiness 状态更新
```

模块边界与控制面分层见[控制面架构](ARCHITECTURE.zh-CN.md)；任务拆分和
兼容迁移见[重构与任务计划](REFACTOR-PLAN.zh-CN.md)。

完成标准：

- 有稳定 schema/API；
- 有本地单测和集成测试；
- 有真实失败路径；
- 有 CLI/CRD 用户入口；
- 有 proof/证据；
- 文档与实际行为一致；
- 没有“文件存在即成功”的降级。

不依赖外部访谈；以可重复测试和真实 kind/GPU 环境为准。

## 1. 工程分层

Python 核心：

- `src/shardferry/readiness.py`：DCP inventory/readiness；
- `src/shardferry/drill.py`：restore drill；
- `src/shardferry/rescue.py`：restore/restart/migrate/hold；
- `src/shardferry/state/`：已有 state bundle；
- `spec/v1/`：CheckpointRecord/Readiness/Drill/Rescue schema；
- `src/shardferry/commands/`：readiness/drill CLI。

Go 控制面：

- `api/v1/checkpointrecord_types.go`；
- `internal/controller/readiness.go`；
- `internal/controller/rescue.go`；
- `internal/controller/live_rank_split.go`（已有契约）；
- `cmd/ctl/readiness.go`。

数据面：

- DCP/ByteCheckpoint/NVRx/TorchTitan/TorchFT 作为 provider；
- 不重写它们的 save/load/FT；
- 我们负责 provider contract、事务、验证和 proof。

## 2. 阶段与硬验收

### P0：契约冻结

交付：schema、状态机、reason/error code、示例、用户手册。

验收：schema 验证示例；状态互斥可解释；不允许 metadata-only 成功；
没有隐式 fallback。

### P1：本地只读 inventory + readiness

交付：扫描 DCP `.metadata`/local ranks；`complete/fresh/digest`；
`shardferry readiness --checkpoint PATH --json`。

验收：缺 rank、重复 rank、截断、不可读、版本不一致全部识别；不读全量
tensor；source 哈希不变；1000 个逻辑 checkpoint 的 p95 查询 <= 2 秒；
状态变化 <= 30 秒可见。

### P2：Restore drill

交付：`shardferry restore-drill`；真实 model/optimizer load；至少 N step；
state bundle 闭包；drill proof。

验收：不是只查 `.metadata`；model/optimizer/RNG/sampler 完整；N step 与
baseline 对比；失败不能标 `READY`；`stateComplete=false` 明确失败。

### P3：Kubernetes inventory/readiness

交付：agent 只读发现本机 root；controller 聚合 CheckpointRecord；
`shardferryctl readiness`/`describe`；agent/controller 重启恢复。

验收：128 rank selector 齐全；agent 重启不丢 inventory；controller 重启
不重复/不误报；`--json` schema 稳定；失败输出 `Next:`。

### P4：Rescue decision

交付：通用 K8s event watcher；RescueDecision；决策依据；Kueue/Volcano
adapter 可选。

验收：事件后 5 秒内决策（缓存可用）；不可恢复明确 hold；没有 Kueue 也能
工作；不允许 split-brain/双训练；决策有 reason/proof。

### P5：Static mobility

交付：已有 static DCP 迁移接入 readiness/decision；cross-mesh/local-disk；
target-ready 成功门禁；journal/atomic commit/resume。

验收：source digest 不变；target 真实加载并跑 N step；任何 rank 失败不能
整体成功；失败可续跑；不允许半状态 target。

### P6：LRS / live

交付：LRS controller 契约；按需 worker；`maxInFlight`；coverage/quorum/
state closure；source-death takeover。

验收：4 GPU 单节点 reconfigure；2 节点 A/B 无共享存储；成功场景
`steps_lost=0`、`global_stop=0`；active rank 不用 async/双模型；失败 fail
closed，不静默回退 static。

### P7：Provider 与 adapter

交付：StateProvider/Loader/Transport 接口；DCP/ByteCheckpoint/NVRx/
TorchTitan/TorchFT 适配；Kueue/Volcano/Kubeflow adapter。

验收：换 provider 不改核心状态机；统一错误；第三方类型不渗透核心；
adapter 失联不影响基础 readiness。

### P8：规模、故障、发布

交付：128 rank/10k checkpoint；agent/controller restart、source death、
target failure、网络分区、磁盘满、checksum 错、stale shadow；RBAC/mTLS/
审计/support bundle；release/upgrade/rollback。

验收：所有故障要么完整 proof 成功，要么 fail closed；不允许静默成功；
关键指标/timeline/proof 可复核；check_release、kind、4 GPU、2 节点矩阵通过。

## 3. 测试环境

- CPU 本地：schema、readiness、drill、resume、rescue unit/integration；
- kind：controller/agent/selector/restart；
- 4 GPU 单节点：NCCL、TP/PP、reconfigure；
- 2 节点：跨集群、无共享存储、source death；
- 规模：128 rank selector、10k checkpoint inventory。

所有门禁必须可在 CI 或显式 runbook 重复执行，不允许只靠口头确认。

## 4. 发布与维护

发布前：P0-P8 全部通过；无未实现占位状态；文档/CLI/CRD/Helm 版本一致；
真实失败矩阵有证据；support/upgrade/rollback 可执行。

发布后：只修 bug、安全、兼容和用户阻塞；新能力重新走产品定义和验收，
不混入维护版本。

## 5. 立即执行顺序

1. P0 schema/状态机；
2. P1 本地 readiness；
3. P2 restore drill；
4. P3 Kubernetes readiness；
5. P4 rescue；
6. P5 static mobility；
7. P6 LRS；
8. P7 providers/adapters；
9. P8 规模/故障/发布。

不允许先做 LRS、后补 readiness；也不允许用 migration 成功代替
checkpoint 可恢复性。
