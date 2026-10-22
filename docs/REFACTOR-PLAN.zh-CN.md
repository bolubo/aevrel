# shardferry 控制面重构与任务计划

状态：执行计划（2026-10-22）
目标：把现有 migration-centric 代码平滑演进为 checkpoint control plane

## 0. 原则

- 不做一次性大搬迁；
- 先加接口/兼容层，再改调用方，最后删除旧路径；
- 每一步有测试，不靠人工口头确认；
- Go `internal/control/` 不依赖具体 provider；
- `CheckpointMigration` 保留兼容，但不再成为新功能入口；
- 旧代码只要仍被用户使用，就保留 shim；
- 迁移、LRS、Kueue 都降为子模块/adapter。

## 0.1 语言边界

- 控制面权威：Go（CRD/API/controller/readiness/rescue/transaction/policy）。
- 训练运行时：Python（DCP/PyTorch/inventory/drill execution/state/LRS data plane）。
- HTTP/JSON schema 是跨语言契约；Python 不写核心 status，Go 不碰 torch。
- 新增控制面功能默认 Go；新增 DCP/训练运行时适配默认 Python。

## 1. 现状到目标

| 现状 | 目标位置 | 处理 |
|---|---|---|
| `src/shardferry/dcp/` | `local/dcp` | 先做 adapter，不搬文件 |
| `src/shardferry/planning/` + `executor.py` | `mobility/` | 包一层 transaction，不重写算法 |
| `src/shardferry/state/` | `state/` | 保留，接 readiness/provider |
| `src/shardferry/verification/` | `verification/` + Go `internal/control/proof` | Python 继续做数据面校验，Go 持有 proof contract |
| `src/shardferry/agent/` | `agent/` | 加只读 scanner/reporter |
| `src/shardferry/lrs/` | `mobility/live` | 后移；先保持 |
| `api/v1/checkpointmigration_types.go` | `api/v1/checkpointmigration_types.go` | 兼容资源 |
| `internal/controller/reconciler.go` | `controller/{readiness,drill,rescue,mobility}.go` | 先拆 router，再拆状态机 |
| `cmd/ctl/migrate.go` | `cmd/ctl/{readiness,drill,rescue,migrate}.go` | 新增命令优先，旧命令保留 |

## 2. 新增模块

```text
src/shardferry/local/
  models.py       # 本地事实/JSON 适配，不是控制面权威
  errors.py       # 本地错误映射
  provider.py     # 本地 provider protocol
  dcp.py          # DCP adapter
  readiness.py    # 本地便捷评估，Go 为权威

src/shardferry/mobility/
  transaction.py  # static/live orchestration
  static.py       # existing planning/executor adapter
  live.py         # LRS adapter

internal/control/
  readiness.go    # 权威 readiness
  policy.go       # freshness/target/rescue policy
  proof.go        # proof contract

api/v1/
  checkpointrecord_types.go
  restoredrill_types.go
  rescuedecision_types.go
  mobilitytransaction_types.go
```

## 3. 任务分解

### A0：契约与模型

#### A0.1 schema

文件：

- `spec/v1/checkpoint-record.schema.json`
- `spec/v1/readiness.schema.json`
- `spec/v1/restore-drill.schema.json`
- `spec/v1/rescue-decision.schema.json`
- `spec/v1/mobility-transaction.schema.json`

验收：

- 示例通过 schema；
- 状态、reason、error code 都有定义；
- 不允许未定义字段；
- `CheckpointMigration` 映射规则写清楚。

#### A0.2 Go 权威领域模型

文件：`api/v1/*_types.go`、`internal/control/models.go`。

验收：

- CRD schema 是权威；
- 状态/枚举/错误码单测；
- controller 不依赖具体 provider；
- CRD 与 JSON schema 一致。

#### A0.3 Python 本地适配模型

文件：`src/shardferry/local/models.py`。

验收：

- 只作为数据面/本地 CLI 适配；
- 不拥有 controller 状态；
- 与 schema 兼容；
- 不导入 Go 或 controller 代码。

### A1：本地只读 readiness

#### A1.1 Provider protocol

文件：`src/shardferry/local/provider.py`。

接口：`discover`、`inventory`、`verify`、`load`、`drill`、`capabilities`。

验收：fake provider 能驱动全部 contract；core 不出现 DCP 分支。

#### A1.2 DCP state provider

文件：`src/shardferry/local/dcp.py`。

复用 `local_dcp_ranks`、`inventory_dcp_descriptors`、metadata read；
只读 metadata/range，不读全量 tensor。

验收：缺 rank/重复/截断/不可读/版本不一致全部识别。

#### A1.3 readiness CLI

文件：`src/shardferry/commands/readiness.py`，`cli.py` 注册。

命令：

```bash
shardferry readiness --checkpoint PATH --json
```

验收：与 ground truth 100% 一致；source digest 不变；p95 <= 2s（1000 记录）。

### A2：Restore drill

#### A2.1 drill core

文件：`src/shardferry/local/drill.py`。

复用 `target_load`/`DcpTargetReady`/state bundle；
真实 model/optimizer load + N step。

验收：metadata-only 必失败；stateComplete=false 必失败；proof 可复核。

#### A2.2 drill CLI

命令：

```bash
shardferry restore-drill --checkpoint PATH --target-world N --steps N --json
```

验收：失败非零；proof 写入 artifact/本地；不修改 source。

### A3：Kubernetes readiness

#### A3.1 CRD

新增 `CheckpointRecord`、`RestoreDrill`、`RescueDecision`、
`MobilityTransaction`；`CheckpointMigration` 保留。

验收：CRD schema、RBAC、deepcopy、Helm CRD、示例全部生成/校验。

#### A3.2 controller readiness

新增 `internal/controller/readiness.go`；`reconciler.go` 只做 dispatch。

验收：record status 唯一权威；agent/controller 重启不丢状态。

#### A3.3 agent scanner

agent 增加只读 scan/report；不把 checkpoint 数据写入状态。

验收：128 rank selector 完整；重启恢复；source 只读。

#### A3.4 ctl readiness

新增 `shardferryctl readiness` / `describe` 的 readiness 段。

验收：人读/JSON 稳定；失败输出 `Next:`。

### A4：Rescue decision

#### A4.1 event source

通用 K8s event watcher；Kueue/Volcano/Kubeflow adapter 可选。

验收：事件后 5 秒内 decision；无 adapter 也能工作；不可恢复 hold。

#### A4.2 decision policy

freshness/complete/loadable/movable/RTO -> restore/restart/migrate/hold。

验收：每个 decision 有 reason/proof；不允许双训练/split-brain。

### A5：Mobility transaction

#### A5.1 transaction wrapper

把现有 `planning/executor` 包成 `MobilityTransaction`；不重写算法。

验收：static 迁移行为不回归；proof 统一。

#### A5.2 target-ready gate

migration 成功必须由 target-ready 证明，不接受文件存在。

验收：target job N step；失败 phase target-failed；不误报。

#### A5.3 CheckpointMigration compatibility

旧 CR 映射到 MobilityTransaction；新增字段只走新资源。

验收：旧 examples 可运行；新功能不依赖旧 CR。

### A6：LRS / live

#### A6.1 worker/provider

按需 worker；复用 LRS CPU4/transport/state；controller 只通过接口调用。

验收：4 GPU reconfigure；2 节点 A/B；source death；fail closed。

### A7：Providers / adapters

- DCP、ByteCheckpoint、NVRx、TorchTitan、TorchFT provider；
- Kueue/Volcano/Kubeflow 只读 adapter；
- conformance suite；
- capability negotiation。

验收：新增 provider 不改 core；adapter 失联不影响 readiness。

### A8：规模与发布

- 128 rank/10k checkpoint；
- agent/controller restart、source death、target fail、网络分区、
  磁盘满、checksum、stale shadow；
- RBAC/mTLS/审计/support bundle；
- release/upgrade/rollback；
- kind/4 GPU/2 节点矩阵。

验收：完整 proof 或 fail closed；无静默成功；所有 gate 可重复执行。

## 4. 并行与依赖

可并行：

- A0 schema/domain 与 A2 drill 的接口设计；
- A1 Python readiness 与 A3 Go CRD 定义；
- A4 event policy 与 A5 transaction 包装。

不可并行/必须先做：

- A0 完成前不写 controller；
- A1 完成前不做 rescue；
- A5 完成前不做 LRS worker；
- conformance suite 不通过不能接 provider。

## 5. 测试矩阵

| 层 | 测试 |
|---|---|
| Python core | schema、状态机、readiness、drill、rescue、proof |
| Provider | fake contract、DCP 完整/缺失/截断、capability |
| Go | CRD validation、controller restart、dispatch、status |
| kind | agent/controller/selector/target-ready/rescue |
| GPU | 4 GPU NCCL/TP/PP/reconfigure |
| 跨域 | 2 节点、无共享存储、source death |
| 规模 | 128 rank、10k record |

## 6. 重构安全规则

- 每次只搬一层；
- 旧 import 先保留 shim；
- 新旧路径同时跑测试；
- 不在 LRS/TLS/发布分支做大规模移动；
- 不删除旧 CRD 直到新资源稳定；
- 不把“目录整齐”作为验收目标。

## 7. 完成定义

发布候选必须满足：

- A0-A8 全部通过；
- 无未实现占位状态；
- 新 provider 可插拔；
- adapter 不侵入核心；
- readiness 是唯一权威；
- 所有成功有 proof，所有失败 fail closed；
- 文档、CLI、CRD、Helm、版本一致。

之后进入维护模式：只做 bug、安全、兼容和用户阻塞；新能力重新走
产品定义和验收。

## 8. 当前落地切片（2026-10-22）

- A0.1：已加入 CheckpointRecord/Readiness/RestoreDrill/RescueDecision/
  MobilityTransaction JSON schema；
- A0.2：已加入 Go `internal/control` 权威 readiness 模型和单测；
- A0.2：已加入 Go `api/v1` 四个控制面资源类型和 CRD；
- A1.1/A1.2/A1.3：已加入 Python `local` DCP inventory adapter 和
  `shardferry readiness` 本地 CLI；
- A2：已加入 target-command `shardferry restore-drill`，要求真实
  model/optimizer load、N step 和 target-ready report；
- A3：已加入 `CheckpointRecord` Go readiness reconciler、agent
  `/v1/checkpoint-record` 只读扫描和 `shardferryctl readiness`；
- A2.2：已加入多 rank `RestoreDrill` reconciler，观测已完成 target Job
  的 per-rank report，并回写 CheckpointRecord；
- A3.2：CheckpointRecord 支持 `agentEndpoint`，controller 自动拉取
  agent facts；agent token 可复用 controller token 或 Secret；
- A2.3：`RestoreDrill` 支持模板创建 Indexed Job（或观测 `jobName`），
  强制 one-pod-per-rank、ownerRef、target-ready 注入与失败关闭；
- A3.3：`CheckpointRecord.agentSelector` 支持多 agent Pod 只读扫描、
  128-rank 合并、重复/缺失/冲突检测；agent 不可达时保留最后事实；
- A4：`RescueDecision` 控制器和通用 Kubernetes Warning event watcher；
  restore/restart/migrate/hold 均由 readiness + 显式 fallback 决定，
  不依赖 Kueue；
- drill proof 现在绑定 checkpoint version + metadata digest；版本或 digest
  改变会使旧 `loadable=true` 失效，不能跨 checkpoint 复用；
- 示例：`examples/checkpoint-control/`；
- A5.0：`MobilityTransaction` 兼容投影已接入 `CheckpointMigration`，
  统一映射 phase、proof、target-ready 和 condition；不改写旧迁移算法；
- A5.1：`MobilityTransaction.spec.migrationSpec` 会创建同名、ownerRef 绑定的
  `CheckpointMigration`，仍由原 planner/executor 执行；未受控同名迁移不会被接管；
- 尚未做：multi-rank local drill、完全脱离 CheckpointMigration 的新执行器、
  LRS worker 接入。
