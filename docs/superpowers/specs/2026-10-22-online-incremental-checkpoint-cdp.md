# shardferry 在线增量 checkpoint CDP

状态：设计中，第一版实现中  
日期：2026-10-22  
范围：在线增量迁移（checkpoint CDP）  
不包含：CRIU/GPU 进程热迁移、任意 checkpoint 格式、训练 runtime 内部改造

## 1. 结论

在线增量迁移不是“定时 rsync checkpoint”。第一版把 checkpoint 建模为：

```text
不可变版本目录 -> 版本 manifest -> 内容寻址 chunk -> shadow target -> promote
```

目标语义：

> 训练不中断地产生 checkpoint 版本；目标集群持续跟随最新完整版本，只传输
> 变化 chunk；切流时选择一个已验证版本，在可配置的暂停上限内 promote；
> 失败可以 resume source，且 target 不会出现混合版本。

## 2. 第一版硬约束

- source checkpoint 必须是不可变版本目录，例如
  `/checkpoints/job/step-1000/`、`/checkpoints/job/step-2000/`。
- 每个版本必须有 `.shardferry-complete` 完成标记；没有标记的版本不参与迁移。
- 版本目录一旦出现完成标记，不允许再改写。原地覆盖不支持；需要 reflink /
  snapshot / 新目录。
- source 永不删除；target 旧版本按 retention 策略回收。
- 一个 promote 只能对应一个 `version_id` + `manifest_root`。
- 第一版不做 CRIU、GPU 进程热迁移、训练 runtime 内部增量 checkpoint。

## 3. 核心对象

### 3.1 VersionMarker

`.shardferry-complete` 是版本的完成证明：

```json
{
  "version": "1",
  "version_id": "step-2000",
  "job_uid": "job-42",
  "step": 2000,
  "parent_id": "step-1000",
  "metadata_hash": "sha256:...",
  "created_at": "2026-10-22T00:00:00Z",
  "complete": true
}
```

`version_id` 必须在同一 checkpoint 流内唯一。`parent_id` 可以为空，但版本链
必须无环。

### 3.2 ChunkRef

```json
{
  "digest": "sha256:...",
  "size": 4194304,
  "file": "rank0/w.distcp",
  "offset": 0,
  "length": 4194304
}
```

`digest` 是 chunk 内容的 SHA-256。相同内容在不同版本、不同文件、不同 rank
之间共享同一个 CAS 对象。

### 3.3 VersionManifest

```json
{
  "version": "1",
  "version_id": "step-2000",
  "parent_id": "step-1000",
  "metadata_hash": "sha256:...",
  "created_at": "...",
  "root": "sha256:...",
  "chunks": []
}
```

`root` 是对规范化 manifest 内容（含 version_id、parent_id、metadata_hash、
chunk 列表）计算的 SHA-256。target promote 必须校验 root。

## 4. Target 侧模型

```text
target/
  cas/sha256/<prefix>/<digest>       # 不可变 chunk
  versions/<version_id>.json          # manifest
  shadow/<version_id>/               # 由 CAS materialize 的 shadow checkpoint
  current                             # 原子 promote 的版本指针
```

promote 是元数据操作：

1. 确认目标 manifest 的所有 chunk 都在 CAS；
2. 校验 `manifest.root`；
3. 将 shadow 目录 materialize 到版本目录；
4. 原子切换 `current`（或原子 rename 版本目录）；
5. 写入 promote 结果。

失败只影响 shadow 版本，不影响已 promote 的版本。

## 5. 增量算法

对每个新 source 版本：

1. 读取 source version manifest；
2. 生成 target 布局下的 canonical chunk；
3. 与上一版 target manifest 比较 digest；
4. 只传输 `current - previous` 的 chunk；
5. 复用 target CAS 中已有的 chunk；
6. 写入新 manifest；
7. 校验 chunk 覆盖、文件大小、manifest root。

关键点：delta 必须在 **target 布局**上计算，不能直接比较 source 文件字节。
这样跨 world size、跨 mesh、跨 rank 的迁移才有一致语义。

第一版 chunk 边界可用固定大小块；第二阶段加入 content-defined chunking，
降低 checkpoint 内部偏移变化导致的传输放大。

## 6. Cutover 协议

目标不是“零暂停”，而是“有界暂停”：

```text
following -> catching-up -> ready -> source-paused
          -> final-delta -> target-verified -> promoted
          -> target-ready -> source-drained
```

规则：

- 目标落后超过 `maxLagVersions` 时拒绝 cutover；
- 目标没有完整 manifest root 时拒绝 cutover；
- source pause 后才允许发最后一个 delta；
- target 校验失败则 resume source；
- promote 成功后 target job 启动失败则回滚 source；
- 旧 controller / 旧 source job 必须被 fencing token 拒绝写入；
- 任何阶段都不允许 target 出现混合版本。

第一版 cutover hook：

- Kubernetes：Job suspend/resume 或用户提供的 pause/resume 命令；
- 非 K8s：显式 `pause`/`resume` 脚本；
- 不在第一版实现 CRIU。

## 7. RPO / RTO

- **RPO**：目标最多落后一个已完成的 checkpoint 版本。
- **RTO**：source pause 到 target ready 的时间，受 `maxPauseSeconds` 限制。
- **传输放大**：`delta_bytes / changed_bytes`，目标 <= 1.2x。
- **正确性**：每个 promote 版本 bitwise 一致；manifest root 一致。

## 8. 验收目标

### 功能

- 连续 5 个 checkpoint 版本，第一版全量，后续只传 delta；
- 目标可从任意完整版本 materialize DCP；
- promote 一个版本后，target 不出现其他版本的 chunk；
- source 被 overwrite 或版本链分叉时明确失败。

### 故障

- 版本只写了一半；
- source job 重启；
- target agent 在 materialize/promote 中被杀；
- controller 在 catch-up/final-delta/promote 中被杀；
- 网络分区；
- 重复 promote；
- 旧 controller 复活；
- CAS chunk 缺失或损坏。

### 规模

- 128 ranks；
- 1GB / 10GB 先做，1TB 作为 GPU 集群目标；
- 3–5 个版本，变化比例 1% / 10% / 50%；
- 记录 delta ratio、source IO 开销、target lag、cutover pause、target load time。

## 9. 实施顺序

1. 版本 marker、scanner、manifest、CAS；
2. 固定块 chunk + manifest delta；
3. shadow materialize + manifest root 校验；
4. follower 状态机、后台追赶、lag；
5. cutover pause/resume + promote/rollback；
6. fencing、故障矩阵、形式化状态机；
7. content-defined chunking、性能与规模；
8. CLI/CRD 产品面和独立验收。

每一步都必须有可重复的测试和硬指标，不能只用“能跑”作为完成标准。
