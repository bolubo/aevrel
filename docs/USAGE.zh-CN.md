# Aevrel 使用场景与验收口径

日期：2026-10-23
适用版本：`0.3.0`

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
| 128 个节点各自 local disk，没有共享路径 | `aevrelctl migrate + agentSelector` | 已有产品入口，待真实多节点验收 |
| 训练不停机在线切换 | live-rank-split 契约 + CPU contract worker | 还不能，GPU 和跨机验证未闭环 |

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
aevrel demo
```

`demo` 成功说明 Python、文件写入、计划执行和 bitwise 校验链路正常。

### 第二步：只读检查

```bash
aevrel doctor \
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
[ok  ] work-dir: /data/job42/checkpoint.aevrel
```

不满意时直接按 `hint` 修，不要先跑迁移。`doctor` 不创建目标目录。

### 第三步：迁移

1D `Shard(0)` 的常见 FSDP 场景：

```bash
aevrel migrate \
  --checkpoint /data/job42/checkpoint \
  --target-world 3 \
  --output /data/job42/checkpoint-3rank
```

2D mesh：

```bash
aevrel migrate \
  --checkpoint /data/job42/checkpoint \
  --source-mesh-shape 2,2 \
  --source-placements "Shard(0),Shard(1)" \
  --target-mesh-shape 3,1 \
  --target-placements "Shard(0),Replicate()" \
  --output /data/job42/checkpoint-3rank
```

用户会看到：

```text
aevrel: reading DCP metadata and indexing local shards
aevrel: planning 4 -> 3 target ranks
aevrel: transferring ...
aevrel: verification passed; writing target DCP
aevrel: migration complete

Migration complete
  checkpoint:   /data/job42/checkpoint
  output:       /data/job42/checkpoint-3rank
  topology:     4 -> 3 ranks
  plan:         ... chunks, ...
  moved:        ...
  verification: bitwise passed
  work dir:     /data/job42/checkpoint.aevrel
```

机器调用加 `--json`；不要在 `--json` 里混入进度文本。

### 第四步：目标侧加载

用 **target world size = 3** 启动训练代码，按目标 DeviceMesh 构造 model/optimizer
state，再走正常 DCP load。`scripts/gpu_target_load.py` 是一个 4 -> 3 FSDP
加载样例。

不要把输出当成单文件 `torch.load` checkpoint；它是 DCP 目录。

### 中断和失败

- 直接重跑同一条命令；work dir 的 journal 会复用已完成节点；
- 不要先删 `checkpoint.aevrel`；
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

长时间训练 checkpoint 可能远大于 demo。当前 `doctor` 的磁盘预估仍是
近似值，真实大 checkpoint 前应先手工确认挂载点剩余空间。

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
4. 让 `metadataOwnerRank` 对应的 source 节点能看到 `.metadata`；controller 会
   把这份小文件分发给其他 source agents，不需要共享 checkpoint 目录；
5. target 节点提供 `/targets` hostPath。

示意：

```bash
kubectl create secret generic aevrel-agent-token \
  --from-literal=token='replace-me'

helm upgrade --install aevrel deploy/helm/aevrel \
  --set source.checkpointHostPath=/data/aevrel/job42 \
  --set source.metadataHostPath=/data/aevrel/job42-metadata \
  --set target.enabled=true \
  --set target.checkpointHostPath=/data/aevrel/target
```

source agent 用 9443，target agent 用 9444，可以同节点共存。

### 用户操作

用户只写一个短 YAML：

```yaml
apiVersion: aevrel.dev/v1
kind: CheckpointMigration
metadata:
  name: job42-128-to-64
spec:
  source:
    metadataOwnerRank: 0
    worldSize: 128
    checkpointRoot: /checkpoints
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: aevrel
        app.kubernetes.io/component: source
  target:
    worldSize: 64
    rootTemplate: /targets/job42/rank{rank}
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: aevrel
        app.kubernetes.io/component: target
```

先预览，再创建并等待：

```bash
aevrelctl migrate -f migration.yaml --dry-run
aevrelctl preflight -f migration.yaml
aevrelctl migrate -f migration.yaml --wait
```

`preflight` 会连接集群，按 selector 查 Pod、调用 source agent 的
`/v1/ranks`，并打印实际发现的 source/target rank 数；它不创建 CR。

controller 会：

1. 用 label selector 找到 ready agent Pod；
2. 让每个 source agent 上报本机 rank；
3. 组合完整 source rankMap；
4. 把 target rank 确定性轮转分配到 target agents；
5. 执行 inventory、plan、stage、commit；
6. `--wait` 打印 rank/字节进度条（LRS 为 coverage/in-flight/quorum）到
   succeeded/failed；失败时给出 `Next:` 动作。

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
- 真实 128 节点上的 metadata transfer 端到端；
- 多租户 selector 隔离；
- 镜像仍需自行构建/推送，Python 也还没有公开发布 wheel。

## 4. 场景 C：训练不停机在线切换（实验）

当前唯一用户入口是 `CheckpointMigration` 的 `mode: live-rank-split`。
操作顺序、字段和验收边界见
[Live Rank Split 用户流程](LIVE-RANK-SPLIT-USAGE.zh-CN.md)。

该路径仍是实验能力：CPU contract worker、controller 状态机和
`target-ready` 门禁已经接通，但真实 GPU collective、跨机训练连续性和
端到端 RTO/RPO 尚未验证。目标侧必须真实加载 model/optimizer 并完成训练
step，不能把“文件已到 target”当作成功。

## 5. 当前产品判断

按“一个人拿到就能恢复 DCP”看：

- 三命令入口、只读检查、bitwise 校验、续跑和 JSON 已经够用；
- 还缺公开发布包、精确磁盘预估、目标侧一键 load 验证。

按“一个人处理真实 128 卡 local-disk 集群”看：

- selector、rank 发现、rankMap 自动生成和 `aevrelctl migrate --wait` 已补齐；
- Dockerfile/Helm 入口已补齐；metadata 可由 `metadataOwnerRank` 通过
  source-agent metadata transfer 分发，不再要求所有 source 节点共享目录；
  公开发布镜像和真实多节点验收仍未完成；
- 因此现在可以给内部平台用户试用，还不能宣称生产级完成。
