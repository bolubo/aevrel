# shardferry 架构：Training State Control Plane

状态：架构冻结候选
定位：训练状态的事实、判断、决策、事务和证明

## 0. 一句话

```text
常驻控制面持续回答：训练状态是否完整、是否新鲜、是否可加载、是否可迁移。
checkpoint 是第一个完整 vertical；迁移、restore drill、LRS 都是控制面发出的
短生命周期动作。
```

迁移不是主资源，也不是 controller 的中心。主资源是 `CheckpointRecord`。

## 1. 架构原则

1. **State before action**：先有事实状态，再决定动作。
2. **Persistent control, ephemeral data**：controller/agent 常驻，数据面按需。
3. **Provider-neutral**：DCP、ByteCheckpoint、NVRx、TorchTitan、TorchFT 都是 provider。
4. **Proof-first**：任何成功都必须有可复核的 proof。
5. **Fail closed**：缺状态、缺 quorum、缺 coverage、缺 target load 都不能成功。
6. **No hidden fallback**：live 失败不能静默改 static；static 失败不能假装成功。
7. **No provider leakage**：第三方类型、错误、生命周期不能进入核心状态机。
8. **Small kernel, many adapters**：核心小，扩展靠 interface。

## 1.1 Go / Python 边界

Go 是控制面权威实现：

- CRD/API、controller、leader election、watch/reconcile；
- CheckpointRecord/Readiness/RescueDecision 状态机；
- policy、quorum/coverage、事务和 proof 引用；
- `shardferryctl`、gateway、event adapters、target-ready 门禁。

Python 只负责训练运行时和数据面：

- DCP/PyTorch metadata、rank/local shard inventory；
- restore drill 的实际 model/optimizer load；
- state bundle、RNG/sampler、LRS 数据面；
- `shardferry` 本地一次性 CLI 和 Python agent。

边界：

- Go controller 不 import torch，不解析 DCP 内存对象；
- Python adapter 不写 CR status，不拥有 readiness 决策；
- 两边只通过稳定 JSON schema/HTTP 接口交换事实和结果；
- Python 可以给出 `inventory`/`complete` 事实，Go 决定是否 `READY`。

这条边界从现在开始执行；后续新增 control-plane 功能默认先用 Go。

## 2. 领域模型

### CheckpointRecord

事实来源，一条记录表示一个 job 的一个 checkpoint version：

```text
CheckpointRecord.spec
  jobRef, namespace, uid
  version, step
  provider, formatVersion
  source topology, rank roots
  agentEndpoint / agentSecretRef (optional read-only discovery)

CheckpointRecord.status
  inventory: ranks, shards, bytes, metadata digest
  readiness: ReadinessStatus
  lastVerifiedAt, lastDrillRef
  conditions, observedGeneration
```

### Readiness

不单独做成可写 CRD；它是 `CheckpointRecord.status` 的派生状态，避免多个
相互矛盾的源头。

```text
ReadinessStatus
  complete: bool
  fresh: bool
  loadable: bool
  movable: bool
  status: READY|STALE|INCOMPLETE|TARGET_BLOCKED|UNVERIFIED
  reasons: []Reason
  estimatedRTOSeconds
  targetCompatibility
```

### RestoreDrill

显式的真实恢复验证，不是定时任务：

```text
RestoreDrill.spec
  recordRef, version, targetWorldSize, steps
  jobName (observe) | jobTemplate (create Indexed Job)
  checkpointTemplate, stateTemplate/providers
RestoreDrill.status
  phase, jobName, startedAt/completedAt
  modelLoaded, optimizerLoaded, stateComplete
  stepsRan, ranksReady/ranksTotal/missingRanks
```

`loadable=true` 只在该 drill 的 version 与 metadata digest 仍匹配当前
CheckpointRecord 时有效。新 checkpoint、新 digest 或新的失败 drill 都会
使旧的 load proof 失效。

### RescueDecision

事件后的动作建议，不直接改调度器：

```text
RescueDecision.spec
  event, workloadRef, checkpointRef
RescueDecision.status
  decision: restore|restart|migrate|hold
  reasonCode, reason, target, proofRef
```

基础入口是通用 Kubernetes Warning event（Evicted/Preempted/JobFailed 等）；
workload 通过 label/annotation opt-in。Kueue/Volcano 只提供更丰富的
workload adapter，不是 rescue 的前置条件。Decision 只做建议，不自动启动
第二份训练，避免 split-brain。

### Proof

不可变、内容寻址的证据，不在 CR 里塞大对象：

```text
Proof
  source digest, target digest
  coverage, quorum, epoch
  drill/migration timeline
  provider versions, timestamps
```

## 3. 控制闭环

```text
scan
  -> CheckpointRecord
  -> Readiness
  -> RescueDecision
  -> RestoreDrill / CheckpointMigration
  -> Proof
  -> Readiness 更新
```

每一步都可以独立失败，但失败不能把下游状态写成成功。

## 4. 分层与模块

```text
CLI / shardferryctl
        |
commands / API / CRD
        |
control: readiness / drill / rescue / policy / proof
        |
providers: state / loader / transport / events
        |
dcp / bytecheckpoint / nvrx / torchtitan / torchft
        |
local disk / HTTP / object storage / Kubernetes
```

### Python 目标结构

```text
src/shardferry/
  local/             # local CLI adapter: DCP inventory/provider/readiness
  mobility/          # static/live transaction orchestration
  state/             # rank-local state bundle
  verification/      # proof, verify, target readiness
  agent/             # read-only inventory + HTTP
  commands/          # user entrypoints
  cli/               # command dispatch
  data/              # existing DCP/planning/executor compatibility
```

现有 `dcp/`、`planning/`、`executor.py` 暂不搬；先加 Python `local/`
adapter 和 Go `api/v1` + `internal/control`，再按重构计划逐步包一层。
禁止一次性大搬迁。

### Go 目标结构

```text
api/v1/
  checkpointrecord_types.go
  restoredrill_types.go
  rescuedecision_types.go
  checkpointmigration_types.go

internal/controller/
  readiness.go
  drill.go
  rescue.go
  live_rank_split.go             # existing contract
  reconciler.go                  # dispatch only

internal/control/
  models.go
  readiness.go
  policy.go
  proof.go
```

`internal/controller/reconciler.go` 不再承载所有状态机；只做路由和条件聚合。

## 5. Provider 接口

核心只依赖接口，不依赖 DCP/Kueue 等具体类型。

```text
StateProvider
  discover(scope) -> records
  inventory(record) -> ranks/shards/digests
  verify(record) -> integrity result

LoaderProvider
  load(record, target) -> load result
  drill(record, target, steps) -> proof

TransportProvider
  send(plan) -> ack
  resume(snapshot) -> ack
  commit(transaction) -> proof

EventProvider
  watch() -> preempt/evict/fail/maintenance events

ProviderCapabilities
  discover, read, load, async, reshard, state_closure, live, ...
```

要求：

- provider 只能返回统一 result/error；
- provider 不得修改 controller 状态；
- provider 失败必须可判 retryable/terminal；
- 新 provider 必须通过 conformance suite，核心不改。

## 6. CRD/API 边界

| 资源 | 角色 | 是否核心 |
|---|---|---|
| `CheckpointRecord` | checkpoint 事实和 readonly inventory | 是 |
| `RestoreDrill` | 可执行验证 | 是 |
| `RescueDecision` | 恢复动作建议 | 是 |
| `CheckpointMigration` | static/live 执行 action | 是 |
| `CheckpointReadiness` | `CheckpointRecord.status` | 否（不做 CRD） |
| `Proof` | artifact/ref | 否（不做大 CRD） |

API 规则：

- 只有 controller 写 status；
- agent 只写自己的 observation，不写最终 readiness；
- adapter 只读取/建议，不拥有核心状态机；
- `CheckpointMigration` 是当前执行资源；新增语义必须先设计再扩展。

## 7. 部署形态

```text
CLI-only
  shardferry -> local provider -> one-shot transaction

Operator
  controller + agent
  CheckpointRecord/Readiness/RestoreDrill/RescueDecision

Platform
  operator + gateway + Kueue/Volcano/Kubeflow adapters

Live
  operator + on-demand LRS worker
```

常驻组件：

- controller：选主、状态机、API；
- agent：只读扫描、报告、按需被调用；
- gateway：跨集群代理/传输；
- adapters：只读事件和状态。

不常驻：

- LRS worker：仅事务期间；
- migration job：仅动作期间；
- restore drill job：仅用户/平台显式触发。

## 8. 状态机

Readiness：

```text
UNKNOWN
  -> INCOMPLETE
  -> STALE
  -> UNVERIFIED
  -> READY
  -> TARGET_BLOCKED
```

RestoreDrill：

```text
Pending -> Running -> Succeeded | Failed | Cancelled
```

RescueDecision：

```text
Pending -> Decided(restore|restart|migrate|hold) | Blocked
```

Live mode 额外包含 `following/cutover/target-validating`；任何失败都必须
fail closed。

## 9. 依赖方向

```text
CLI/CTL/commands
      |
control
      |
providers (interfaces)
      |
data plane (DCP/planning/executor/transport/lrs)
      |
contracts/manifest/identity
```

禁止：

- `control` 导入 `dcp`/`kueueadapter`/`cmd`；
- provider 导入 controller 或命令层；
- `internal/controller` 出现具体 provider 分支；
- adapter 写核心 status。

## 10. 生长边界

横向：更多 provider（DCP/ByteCheckpoint/NVRx/TorchTitan/TorchFT/Orbax）。

纵向：更多动作（readiness → drill → rescue → static mobility → live）。

斜向：更多控制域（K8s → Slurm/裸金属 → 多集群/多区域）。

生长条件：

- provider 可插拔；
- conformance suite 固定；
- proof/审计数据积累；
- adapter 不侵入核心；
- API 兼容优先于内部整洁。

## 11. 非目标

- 不重写 checkpoint save/load；
- 不重写调度器、存储、CSI、Velero、CRIU；
- 不做通用 dashboard；
- 不把 migration 资源当产品中心；
- 不要求所有用户常驻 agent；
- 不承诺未验证的 live 语义。

## 12. 架构验收

- 新增一个 provider 不修改 Go `internal/control/`；
- 新增一个 scheduler adapter 不修改状态机；
- 同一 record 的 readiness 只有一个权威来源；
- CLI/controller 看到同一状态；
- provider 失败统一映射为错误码；
- 任意成功路径都有 proof；
- 任意故障路径 fail closed；
- `CheckpointMigration` 可兼容，但不成为新功能依赖。
