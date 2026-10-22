# shardferry V1 生产架构设计

状态：等待 owner 评审  
日期：2026-10-21  
范围：GPU 验证之前的 V1 生产闭环  
依据：owner 明确要求不做中间态，直接设计最终态；迭代以天计；必须解决
128 卡 local-disk 真实问题。

## 1. 结论

V1 不从“CLI 先跑通、以后再加 K8s”演进，直接采用最终形态：

```text
用户 / CI -> shardferryctl -> Kubernetes API -> CheckpointMigration CR
                                                   |
                                         shardferry-controller
                                          /        |          \
                                 source agents  target agents  artifact store
                                    (只读)         (写入)       (plan/report)
                                          \        /
                                           \      /
                                     direct HTTP/TLS data plane
```

CLI 只是客户端，不负责生产编排。controller 不代理 checkpoint 数据。数据面由
每个节点上的 agent 完成，且每个节点可以承载多个 rank。

V1 只支持一条黄金路径：Linux/Kubernetes、PyTorch DCP、local disk、1D/2D
DeviceMesh、`Shard`/`Replicate`/`Partial(sum|avg)`、直连 HTTP/TLS range
传输、source 1..256 ranks。其他格式和部署形态明确拒绝。

## 2. 关键决策

- 实现语言固定为 **Go controller + Python agents**：Go 负责 CRD、reconcile、
  workqueue、conditions 和 leader election；Python 负责 PyTorch/DCP、range、
  staging、bitwise 和本地 file I/O。Controller 镜像不携带 PyTorch。
- `CheckpointMigration` CRD 从第一天就是生产入口。
- 一个 migration 只有一个 CR；不创建 128 个 per-rank CR。
- controller 用 work queue 管理 rank，不创建 128 个长连接。
- per-node agent 管理本节点多个 rank，不假设 rank 数等于节点数。
- source agent 只读，target agent 只在显式 target root 写入。
- 全量 plan/report 放内容寻址 artifact；CR status 只放摘要。
- commit 使用全局 barrier + 本地 `os.replace`，不假装跨节点原子事务。
- 旧 CLI 保留为开发/离线工具，不再扩展生产编排职责。

这不是“为了 K8s 而 K8s”，而是 128 卡场景下状态、恢复和可观测性的最低要求。

## 3. 组件

### 3.1 `CheckpointMigration` CRD

Group/version：`shardferry.io/v1`  
Kind：`CheckpointMigration`  
Scope：namespaced

关键 spec：

```yaml
apiVersion: shardferry.io/v1
kind: CheckpointMigration
metadata:
  name: job42-to-small-cluster
spec:
  checkpoint:
    format: dcp
    formatVersion: "1.0.0"
  source:
    metadataOwnerRank: 0
    worldSize: 128
    mesh: {shape: [16, 8], placements: [Shard(0), Shard(1)]}
    rankMap:
      - {rank: 0, endpoint: "https://source-node-0:9443"}
      - {rank: 1, endpoint: "https://source-node-0:9443"}
    checkpointRoot: /checkpoints/job42
  target:
    worldSize: 64
    mesh: {shape: [8, 8], placements: [Shard(0), Shard(1)]}
    rankMap:
      - {rank: 0, endpoint: "https://target-node-0:9443"}
      - {rank: 1, endpoint: "https://target-node-0:9443"}
    rootTemplate: /checkpoints/job42-migrated/rank{rank}
  policy:
    verify: bitwise
    commit: all-or-nothing
    maxConcurrentTransfers: 16
    maxAttemptsPerRank: 5
    timeoutSeconds: 3600
    sourceChunkCacheBytes: 2147483648
  artifacts:
    store: pvc
    claimName: shardferry-artifacts
```

约束：

- metadata owner 必须出现在 source rankMap；
- source/target worldSize、mesh 乘积和 rankMap 覆盖必须一致；
- V1 的 verify 只能是 `bitwise`，commit 只能是 `all-or-nothing`；
- V1 支持 `pvc` 和 `s3` artifact store；checkpoint 数据绝不进 artifact；
- source root 和 target root 不能解析到同一目录。

status 只保存摘要：`phase`、`conditions`、`planRef/planHash`、source/target
ready 计数、staged/committed/failed 计数、总 bytes、失败 rank 和起止时间。
完整 per-rank report 通过 artifact 读取，不塞进 CR。

### 3.2 Controller

controller 负责：校验 CR、调用 source inventory、生成确定性 plan、写入 artifact、
下发 per-rank target task、驱动状态机、汇总 report、处理取消/超时/重试。

controller 不在 API server 创建 per-rank 对象，不读取 checkpoint 数据，不保存
128 个长连接。默认并发 16，使用 worker pool、退避和背压。生产部署使用
leader election；同一时刻只有一个 controller 写同一 migration。

### 3.3 Source agent

source agent 是只读数据面：每节点一个 agent、可承载多个 rank，提供 metadata、
inventory、range read、health 和 metrics。它不写 source，不创建全局 raw
checkpoint，对 DCP `.distcp` 使用有界 chunk cache 提供 tensor byte range。
cache 不足时 preflight 失败，不能静默退化成全量复制。

source inventory 至少包含 `rank`、`format`、`metadata_hash`、每个 chunk 的
tensor/offset/size、文件相对路径、byte range、checksum、torch/agent version。
metadata 由 `metadataOwnerRank` 提供；缺失或 hash 不一致直接失败。

### 3.4 Target agent

target agent 是本节点写入和校验面：每节点一个 agent、可承载多个 target rank。
它接收 controller 的 per-rank task，先做路径/权限/空间/冲突/锁的 preflight，
再拉取必要 range、写 staging、fsync、bitwise 校验，等待全局 commit barrier。
commit 时写入本 rank 的 `*.distcp`，并写入 controller 生成的 `.metadata` 副本；
metadata 是小 artifact，不是 checkpoint 数据，不能因此引入全局 checkpoint。
task 只携带 `specRef`/`planRef`，不携带完整 plan；`execute` 只做 stage，
独立 `commit` 才执行本地原子提交。

target 状态：

```text
created -> preparing -> prepared -> transferring -> staged
        -> commit-ready -> committing -> committed
        -> failed / cancelled / commit-incomplete
```

同一 migration/rank/task hash 必须幂等；target root 加锁，禁止两个 controller
同时写；最终文件只能通过本地原子 rename 提交。

### 3.5 Agent API v1

Agent 使用 HTTPS/JSON，至少提供：

```text
GET  /v1/healthz
GET  /v1/readyz
GET  /v1/capabilities
GET  /v1/inventory?root=...
GET  /v1/metadata
POST /v1/plan
GET  /v1/shards/{chunk_id}?offset=...&length=...
POST /v1/tasks
POST /v1/tasks/{id}/prepare
POST /v1/tasks/{id}/execute
POST /v1/tasks/{id}/commit
POST /v1/tasks/{id}/cancel
GET  /v1/tasks/{id}
GET  /v1/tasks/{id}/report
```

`POST /v1/tasks` 必须接受 `migration_uid/rank/task_hash` 作为幂等键。重复请求
返回同一状态，不重新开始。source agent 只暴露 inventory/metadata/shard read；
target agent 只接受 controller 签发的 task。

### 3.6 稳定契约

V1 至少冻结以下对象：

| 对象 | 关键字段 |
|---|---|
| `Inventory` | version、rank、format、metadataHash、chunks、checksums |
| `Plan` | migrationId、topology、nodes、bytes、fullGather=false |
| `Task` | migrationUid、rank、specRef、planRef、planSlice、targetRoot、commitToken |
| `Report` | rank、phaseTimes、bytes、verification、retry、error |
| `Error` | code、category、retryable、phase、rank、details |

错误码固定为：`INVALID_SPEC`、`UNSUPPORTED_FORMAT`、`VERSION_MISMATCH`、
`SOURCE_UNREACHABLE`、`SOURCE_NOT_FOUND`、`INVENTORY_INCOMPLETE`、
`PLAN_INVALID`、`TARGET_CONFLICT`、`TARGET_LOCKED`、`DISK_FULL`、
`PERMISSION_DENIED`、`NETWORK_TRANSIENT`、`CHECKSUM_MISMATCH`、
`VERIFY_FAILED`、`COMMIT_INCOMPLETE`、`TIMEOUT`、`CANCELLED`、`INTERNAL`。

### 3.7 CLI

生产入口是 `shardferryctl`：

```text
shardferryctl apply -f migration.yaml
shardferryctl get migration
shardferryctl describe migration job42-to-small-cluster
shardferryctl logs migration job42-to-small-cluster --rank 37
shardferryctl plan --spec migration.yaml --dry-run --json
shardferryctl doctor --spec migration.yaml --json
shardferryctl cancel migration job42-to-small-cluster
```

所有命令支持 `--json`、稳定错误码、`retryable` 和非交互执行。`doctor` 只读，
`plan --dry-run` 不创建 target、不修改 source。

## 4. 数据流

1. 用户创建 `CheckpointMigration`。
2. controller 校验 CR 与版本，并调用 source agents 的 inventory。
3. controller 读取 metadata owner 的 DCP metadata，合并 inventory。
4. controller 生成 plan，写入 artifact，设置 `PlanReady`。
5. controller 让所有 target agents prepare；全部 ready 才进入 transfer。
6. target 从 source agent 直连拉取自己的 ranges，写 staging 并校验。
7. 所有 rank staged 后，controller 写 `CommitIntent` 并发 commit。
8. 所有 rank committed 后，controller 汇总 report，置 `Succeeded`。

任何环节失败都写入 `reason`、`message`、`errorCode`、失败 rank，并保留可恢复
证据。

## 5. 128 卡设计

- controller 使用工作队列，单 migration 只有一个 CR。
- per-node agent 可处理多个 rank；128 rank 可以是 8 节点 × 16 rank。
- source metadata owner 单独提供 tensor schema；其他 agent 只上报本地 chunk。
- target 只拿自己的 plan slice，不下载完整 plan。
- controller 按 source agent 限流，target 遇 429 指数退避。
- 每个 rank 独立 timeout/retry，但 commit 必须等全部 rank。
- CR status 只保留 256 以内 rank 摘要；完整 report 写 artifact。
- 128 rank 的规划、传输、校验、commit 都必须可分别观测。

## 6. 提交与故障语义

V1 使用“全局屏障 + 本地原子提交”：

- prepare 失败：不传输；
- transfer/verify 失败：清理 staging，不产生最终 target；
- commit 前一个 rank 失败：全部不提交；
- commit 中途失败：进入 `CommitIncomplete`，只 repair commit，不重新传输；
- target 节点被驱逐：新 agent 接管同 root；盘丢失则重建该 rank task；
- controller 重启：从 CR、artifact、agent task 恢复；
- 用户取消：commit 前清理 staging；commit 后完成或 repair；
- source 文件变化：checksum 失败，拒绝继续。

`CommitIncomplete` 绝不能显示为成功，也不能被普通 retry 静默覆盖。

## 7. 安全和可运维性

- controller/agent 使用 mTLS；跨 agent 访问使用 per-migration 短期 token；
- token 存 Kubernetes Secret，不写入 CR、日志或 report；
- agent 非 root、只读根文件系统，仅 target root 可写；
- NetworkPolicy 限制端口，禁止默认无认证监听公网；
- JSON 日志含 `migration_uid`、`rank`、`phase`、`error_code`；
- Prometheus 指标含 rank phase、bytes planned/moved、retries、verify failures；
- Event 含 Accepted、PlanReady、AgentsReady、TransferComplete、Verified、
  Committed、Failed；
- 完整 per-rank report 默认保留 7 天。

## 8. 快速迭代

- CRD 从第一天使用 `shardferry.io/v1`，只做 additive change；
- 破坏性变更必须 `v2` + conversion；
- agent 上报 `apiVersion` 和 `capabilities`，controller 支持 N-1 agent；
- controller、agent、CLI 共用一个镜像的不同入口；
- 新能力放 feature gate，默认关闭；
- 每次合并跑契约测试和 128-rank CPU 仿真；
- 每次 release 跑 K8s e2e，GPU 测试按实验卡补跑；
- plan/report/schema 都有版本和 content hash。

状态机、协议和错误码是稳定面；内部实现可以每天改。

## 9. GPU 前验证门禁

### G0 契约

- CRD OpenAPI 通过 `kubectl apply --dry-run=server`；
- Inventory/Plan/Task/Report/Error/schema 全部有测试；
- 所有 JSON 输出可被 schema 校验；
- 错误码和 `retryable` 有文档；
- `doctor` 无副作用，`capabilities` 可读。

### G1 正确性

- unit、property、oracle、mutation 全绿；
- 4→3、4→1、1→4、1D↔2D、Partial source/target 全部 bitwise；
- 128→64、128→256 每个 target byte 恰好覆盖一次；
- plan bytes ≤ theoretical minimum 的 1.2 倍；
- `full_gather=false`；
- 真实 DCP 4→3 在 CPU/gloo 下加载并输出一致。

### G2 CPU/K8s 端到端

- kind 部署 controller、source/target agents；
- 128 source ranks（例如 8 节点 × 16 rank）→ 64/256 target ranks；
- 一个 CR 完成迁移并产生完整 rank report；
- 删除/重建 controller 后任务继续；
- agent 滚动升级不丢状态；
- Helm install/upgrade/uninstall 通过。

### G3 故障矩阵

- source agent 启动前/中途不可达；
- target agent 在 prepare/transfer/verify/commit 各阶段被杀；
- controller 在 plan/transfer/commit 阶段被杀；
- source checksum、target 冲突、权限、磁盘满；
- target 节点 drain；
- commit 前一个 rank 失败，没有最终 target；
- commit 中途失败，`CommitIncomplete` 可 repair；
- 取消和重复 apply 幂等；
- source 文件树 SHA256 全程不变。

### G4 规模和资源

- 128 source ranks，至少 1GB/10GB 数据；
- 记录每 rank bytes、峰值内存、scratch、耗时；
- 不产生全局 raw checkpoint；
- source chunk cache 不超过配置上限；
- 并发 16/32/64 不产生控制面风暴；
- 单 rank 失败不拖垮其他 rank 的传输。

### G5 安全和运维

- 未认证请求被拒绝；
- token/mTLS 轮换不破坏运行中的 migration；
- agent 只写 target root；
- NetworkPolicy 生效；
- 128 rank 的 CR status 小于 1MB；
- Prometheus、Event、日志、report 可关联同一 migration。

### G6 独立验收

- 两个独立用户/团队按 README + Helm 从零部署并完成迁移；
- 不修改源码、不接受口头补丁；
- 保留原始 report、日志和完整命令。

## 10. GPU 阶段与闭环

GPU 不改变 V1 架构，只补执行环境：

- 4 GPU FSDP fp32/bf16 4→3、4→1、1→4；
- model、AdamW、optimizer state、forward 校验；
- 4→3 默认 `torch.equal`，不静默放宽容差；
- 8→5、16→7 可选；
- 记录 CUDA、NCCL、驱动、torch、显存峰值；
- 至少一次 GPU source → GPU target 的真实迁移；
- GPU 测试失败不能靠换卡掩盖。

只有满足以下条件才算 V1 闭环：

1. G0-G6 在固定 Helm chart、镜像 digest、CRD 版本下全绿；
2. 128→64 和 4→3 各至少一次完整成功；
3. `CommitIncomplete`、node drain、controller restart 各至少一次恢复成功；
4. 独立用户按文档完成，不依赖作者现场指导；
5. GPU 矩阵结果写回实验卡；
6. 文档、schema、错误码、支持矩阵和限制同步更新；
7. 没有未关闭的 P0/P1 缺陷。

任何一项失败，V1 不能 freeze。

## 11. 实施边界

不串行建设“CLI 版本再重写 controller”；按最终边界并行推进：

1. 核心库：Mesh/planner/executor/verify 抽成稳定库，补 DCP inventory 和 target metadata。
2. 协议层：CRD、Inventory、Plan、Task、Report、Error。
3. Agent 层：source read-only、target task、chunk cache、lock、journal、report。
4. Controller：reconcile、work queue、commit barrier、artifact。
5. K8s 交付：Helm、RBAC、mTLS、DaemonSet、controller Deployment。
6. 验证层：kind e2e、128 rank 仿真、故障注入、独立用户包。
7. GPU：只替换执行环境，不改协议和状态机。

## 12. 风险

| 风险 | 处理 |
|---|---|
| DCP `.distcp` 解码占用内存 | 有界 chunk cache，preflight 检查，超限失败 |
| 128 rank status 超过 CR 大小 | status 摘要 + artifact 完整 report |
| 128 rank 没有 128 节点 | per-node agent 承载多 rank，显式 rankMap |
| 跨集群网络不可达 | static endpoint、mTLS、preflight，不做自动发现 |
| controller 单点 | leader election、artifact、幂等 reconcile |
| 跨节点 commit 非原子 | 全局 barrier、`CommitIntent`、本地 rename、repair |
| 快速迭代导致协议漂移 | v1 additive only、capabilities、N-1 兼容、契约测试 |

## 13. 需要 owner 批准的范围变更

本设计把原 charter 中“K8s operator/CRD out of scope”改为 V1 核心组成，
以换取 128 卡状态机、恢复和可观测性。批准本设计即视为批准该范围修订；
正式发布前同步更新 `spec/charter.md`。

