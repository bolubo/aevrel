# shardferry 使用场景与验收口径

日期：2026-10-22  
适用版本：`0.1.1`

## 1. 先做一个判断

开始前只问三个问题：

1. source checkpoint 的所有 rank 文件，能不能同时出现在一台机器的路径下？
2. 用户是否知道 source/target 的 world size 和 DeviceMesh？
3. 现在是离线恢复，还是训练进程必须不停？

对应结论：

| 情况 | 当前支持 | 能交给普通用户 |
|---|---|---|
| 单机、NFS、对象存储挂载后能看到完整 DCP 目录 | 是，三条命令 | 可以，前提是先有可安装包 |
| 单机 DCP，2D mesh | 是，但需显式传 mesh/placement | 有条件可以 |
| 128 个节点各自 local disk，没有共享路径 | `shardferryctl migrate + agentSelector` | 已有产品入口，待真实多节点验收 |
| 训练不停机在线切换 | 代码有 CDP/cutover 基础 | 还不能，未形成产品入口 |

## 2. 场景 A：一个人恢复一个 DCP checkpoint

这是当前最完整、最应该先交付的场景。

### 前提

例如一个 4-rank FSDP 任务在 `/data/job42/checkpoint` 留下了：

```text
checkpoint/
  .metadata
  __0_0.distcp
  __1_0.distcp
  __2_0.distcp
  __3_0.distcp
```

这些文件在一台机器上直接可见，或者在 NFS、JuiceFS、CephFS 等共享路径下可见。
不需要 GPU，迁移本身可以在 CPU 上做；目标加载需要相应 GPU/CPU 训练环境。

### 第一步：安装

当前没有公开发布 wheel，用户需要拿到仓库或内部构建产物：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dcp]"
shardferry demo
```

`demo` 成功说明 Python、文件写入、计划执行和 bitwise 校验链路正常。

### 第二步：只读检查

```bash
shardferry doctor \
  --checkpoint /data/job42/checkpoint \
  --output /data/job42/checkpoint-3rank
```

期望看到：

```text
Ready to migrate.
[ok  ] python: ...
[ok  ] dcp-runtime: torch ...
[ok  ] checkpoint: /data/job42/checkpoint
[ok  ] dcp-metadata: ...
[ok  ] dcp-storage: ...
[ok  ] output: /data/job42/checkpoint-3rank
[ok  ] work-dir: /data/job42/checkpoint.shardferry
```

不满意时直接按 `hint` 修，不要先跑迁移。`doctor` 不创建目标目录。

### 第三步：迁移

1D `Shard(0)` 的常见 FSDP 场景：

```bash
shardferry migrate \
  --checkpoint /data/job42/checkpoint \
  --target-world 3 \
  --output /data/job42/checkpoint-3rank
```

2D mesh：

```bash
shardferry migrate \
  --checkpoint /data/job42/checkpoint \
  --source-mesh-shape 2,2 \
  --source-placements "Shard(0),Shard(1)" \
  --target-mesh-shape 3,1 \
  --target-placements "Shard(0),Replicate()" \
  --output /data/job42/checkpoint-3rank
```

用户会看到：

```text
shardferry: reading DCP metadata and indexing local shards
shardferry: planning 4 -> 3 target ranks
shardferry: transferring ...
shardferry: verification passed; writing target DCP
shardferry: migration complete

Migration complete
  checkpoint:   /data/job42/checkpoint
  output:       /data/job42/checkpoint-3rank
  topology:     4 -> 3 ranks
  plan:         ... chunks, ...
  moved:        ...
  verification: bitwise passed
  work dir:     /data/job42/checkpoint.shardferry
```

机器调用加 `--json`；不要在 `--json` 里混入进度文本。

### 第四步：目标侧加载

用 **target world size = 3** 启动训练代码，按目标 DeviceMesh 构造 model/optimizer
state，再走正常 DCP load。`scripts/gpu_target_load.py` 是一个 4 -> 3 FSDP
加载样例。

不要把输出当成单文件 `torch.load` checkpoint；它是 DCP 目录。

### 中断和失败

- 直接重跑同一条命令；work dir 的 journal 会复用已完成节点；
- 不要先删 `checkpoint.shardferry`；
- source、work、output 三者不允许互相包含；
- 未通过 bitwise 校验，不会产生最终 target DCP。

### 磁盘预算

产品 `migrate` 不再抽取 source raw shard；它直接读取 `.distcp` 的 tensor
range。用户至少要预留：

```text
source 原目录（只读）
+ work dir 中的 target staging/final shard
+ output DCP
```

长时间训练 checkpoint 可能远大于 demo。`v0.1.1` 的 `doctor` 还没有做精确空间
预估，真实大 checkpoint 前应先手工确认挂载点剩余空间。

## 3. 场景 B：128 卡 local disk，没有共享文件系统

这是项目真正的 128 卡目标场景。现在的产品入口是 selector 发现，不再要求用户
手写 128 条 rankMap。

数据实际是：

```text
node-00:/local/ckpt/job42/...
node-01:/local/ckpt/job42/...
...
node-127:/local/ckpt/job42/...
```

### 部署

需要平台管理员先做一次：

1. 构建并推送 controller/agent 镜像；
2. 创建 agent token Secret；
3. 用 Helm 在同一批节点启用 source/target agent；
4. 把 source 的 `.metadata` 放在所有 source 节点都能看到的独立路径；
5. target 节点提供 `/targets` hostPath。

示意：

```bash
kubectl create secret generic shardferry-agent-token \
  --from-literal=token='replace-me'

helm upgrade --install shardferry deploy/helm/shardferry \
  --set source.checkpointHostPath=/data/shardferry/job42 \
  --set source.metadataHostPath=/data/shardferry/job42-metadata \
  --set target.enabled=true \
  --set target.checkpointHostPath=/data/shardferry/target
```

source agent 用 9443，target agent 用 9444，可以同节点共存。

### 用户操作

用户只写一个短 YAML：

```yaml
apiVersion: shardferry.io/v1
kind: CheckpointMigration
metadata:
  name: job42-128-to-64
spec:
  source:
    worldSize: 128
    checkpointRoot: /checkpoints
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: shardferry
        app.kubernetes.io/component: source
  target:
    worldSize: 64
    rootTemplate: /targets/job42/rank{rank}
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: shardferry
        app.kubernetes.io/component: target
```

先预览，再创建并等待：

```bash
shardferryctl migrate -f migration.yaml --dry-run
shardferryctl preflight -f migration.yaml
shardferryctl migrate -f migration.yaml --wait
```

`preflight` 会连接集群，按 selector 查 Pod、调用 source agent 的
`/v1/ranks`，并打印实际发现的 source/target rank 数；它不创建 CR。

controller 会：

1. 用 label selector 找到 ready agent Pod；
2. 让每个 source agent 上报本机 rank；
3. 组合完整 source rankMap；
4. 把 target rank 确定性轮转分配到 target agents；
5. 执行 inventory、plan、stage、commit；
6. `--wait` 打印 phase 和 rank counts，直到 succeeded/failed。

每个 target rank 在 commit 后会 finalize 成本地可加载的 DCP 目录：

```text
/targets/job42/rank37/
  .metadata
  __0_0.distcp
  w/rank37.bin        # 原始 staging 产物仍保留作证据
```

需要时可在 target agent 上启用 `--load-smoke`，finalize 后执行一次
rank-local DCP load smoke；默认关闭，避免大 checkpoint 为原子提交增加额外
CPU/IO 成本。

目标训练任务需要按 rank 使用本机 `targetRoot` 下的 DCP 目录；这避免在
local-disk 场景把所有 rank 汇总到一台机器。完整目标任务的 `load` smoke
仍应由目标训练应用执行，不能只相信文件存在。

如果 source rank 缺失、重复，或 selector 没匹配到 ready Agent，迁移会在准备阶段
失败，不会开始提交 target。

### 还没验证的部分

- 真实 128 节点、1GB/10GB 数据面的 selector 端到端；
- metadata 共享路径的部署自动化；
- 多租户 selector 隔离；
- 镜像仍需自行构建/推送，Python 也还没有公开发布 wheel。

## 4. 场景 C：训练不停机在线切换

CDP、fencing、pause、promote/rollback 和 cutover 服务基础已经存在。当前入口
是内部 `cutover-serve`，不是三命令产品面；在真实 GPU/target load 验收前，
仍不要对外宣传“在线热迁移已完成”。

`cutover-serve` 默认每 `--follow-interval` 秒后台追赶 source 完整版本，
持续维护 target shadow；`/v1/status` 会同时返回 `follow_state`、lag 和最近
一次错误。出于排障可以传 `--no-auto-follow`，此时只保留显式
`/v1/follow`：

```bash
shardferry cutover-serve \
  --source /checkpoints/job42 \
  --target /targets/job42 \
  --token "$SHARDFERRY_CUTOVER_TOKEN" \
  --follow-interval 2 \
  --retain-versions 3
```

默认分块策略是 `file`，这是 RTO 优先路径；需要更小的字节增量时可显式传
`--chunk-alg cdc`，需要固定块验证时传 `--chunk-alg fixed --chunk-bytes ...`。

`--retain-versions N` 默认关闭（0）；显式开启后，每轮 follow 成功后会保留
最近 N 个 target 版本，并额外保护 `current` 和 fence 指向的版本。旧 shadow
和 manifest 删除后，只 GC 不再被保留 manifest 引用的 CAS chunk；source
永远不删除。删除前会先检查所有保留版本的 chunk 和 shadow，发现缺失就中止，
不会继续扩大清理。`/v1/status.retention` 返回最近一次删除的版本、chunk 数和
释放字节数。

切流时 `--target-ready-command` 会在 promote 后执行；命令返回非零或超时会
回滚 target pointer 并恢复 source。`--max-pause-seconds` 必须覆盖 final delta
和 target ready，不能把它设得比实际 target 启动时间还短。

仓库提供一个最小真实探针，可直接作为 target-ready command：

```bash
python -m shardferry.target_probe \
  --target-root /targets/job42 \
  --version-id "{version_id}" \
  --timeout 60
```

它等待 target `current` 指向指定版本，并校验 manifest root、CAS chunk 和
shadow 文件。GPU 训练侧的真实 load hook 应在下一步替换/包装这个命令。
`/v1/status.last_cutover` 会保留最近一次 `paused_seconds` 与
`target_ready_seconds`，用于 RTO 报告。

如果 target job 暴露 readiness endpoint，也可以直接使用
`--target-ready-url https://target-job/readyz?version={version_id}`；
2xx 视为 ready，404/409/425/429/5xx 会在 timeout 内重试，其他 4xx 立即失败。
两个参数可以同时提供，执行顺序固定为 checkpoint probe -> target job URL。
如果 target 已经是 DCP 目录，可加
`--target-ready-dcp-checkpoint /targets/job42` 做真实 CPU DCP load；它会占用
目标 state dict 的内存，适合作为 target job 的内部 hook，不作为大模型默认探针。
也可以直接在 target job 内执行：

```bash
python -m shardferry.target_probe \
  --dcp-checkpoint /targets/job42 \
  --version-id "{version_id}"
```

local-disk target 每个 rank 只持有自己的分片时，加 `--dcp-rank N`（或
`cutover-serve --target-ready-dcp-rank N`）：它只反序列化本 rank 的
`.distcp` chunk，不构造 global state dict。

如果用户要的是训练不中断，验收必须额外包含：

- source 暂停窗口；
- 最终 delta 追赶；
- target model/optimizer 真正 load；
- 失败 rollback；
- controller 重启；
- 端到端 RTO/RPO。

这些是另一个产品门禁，不应和离线 `migrate` 混在一起。

## 5. 当前产品判断

按“一个人拿到就能恢复 DCP”看：

- 三命令入口、只读检查、bitwise 校验、续跑和 JSON 已经够用；
- 还缺公开发布包、精确磁盘预估、目标侧一键 load 验证。

按“一个人处理真实 128 卡 local-disk 集群”看：

- selector、rank 发现、rankMap 自动生成和 `shardferryctl migrate --wait` 已补齐；
- Dockerfile/Helm 入口已补齐，但公开发布镜像、metadata 分布自动化和真实多节点验收
  仍未完成；
- 因此现在可以给内部平台用户试用，还不能宣称生产级完成。
