# shardferry 产品面

状态：V1 本地迁移入口  
日期：2026-10-22

## 1. 产品定义

用户只有一件事要做：把一个已经落盘的 PyTorch DCP checkpoint 迁移到新的
world size / DeviceMesh，并确认结果逐字节正确。

用户不需要理解 MigrationSpec、plan node、journal、agent 或 controller。
这些仍属于稳定内部契约，但不是首屏 API。

## 2. 稳定入口

```text
shardferry demo
shardferry doctor --checkpoint PATH
shardferry migrate --checkpoint PATH --target-world N
shardferry capabilities --json
shardferryctl migrate -f migration.yaml --wait
```

### demo

- 无 checkpoint、无 GPU、无网络即可运行；
- 默认写入 `./shardferry-demo`；
- 用于确认安装、文件写入和 bitwise 校验链路正常。

### doctor

- 默认只检查运行环境；
- 传 `--checkpoint` 时检查 Python、PyTorch/DCP、`.metadata`、源文件可达性、
  output/work 路径和目录冲突；
- 严格只读，不创建 output、work 或 target；
- 失败时给出可直接执行的修复提示。

### migrate

一次完成：

```text
读取 DCP -> 生成迁移计划 -> 本地传输 -> bitwise 校验 -> 导出目标 DCP
```

默认路径：

- work dir：`<checkpoint>.shardferry`
- output：`<checkpoint>-migrated`

重跑同一命令会复用 work dir 中已完成的节点。源 checkpoint 始终只读；
source、work、output 三者不允许互相包含。

### shardferryctl migrate

集群 local-disk 场景的产品入口。用户只写 source/target world size、mesh、
checkpoint root、target root 和 agent label selector；controller 自动发现 ready
agent Pod、收集本机 rank、生成 rankMap，并驱动完整迁移。

```bash
shardferryctl migrate -f migration.yaml --dry-run
shardferryctl migrate -f migration.yaml --wait
```

### live-rank-split（实验入口）

同一个 `CheckpointMigration` 增加 `spec.mode: live-rank-split`，由外部
LRS 执行器在稳定 step 边界做 per-rank handoff。Go controller 负责校验、
quorum/coverage、fencing 门禁、target-ready 和状态；不把 LRS 内部 journal
塞进 CR。

```bash
shardferryctl plan -f examples/live-rank-split/migration.yaml
shardferryctl migrate -f examples/live-rank-split/migration.yaml --wait
shardferryctl describe migration live-rank-split-demo --ranks
```

该入口要求 controller 配置 `--live-url`（Helm:
`controller.liveURL`）。没有执行器时 live CR 失败关闭；不会退回静态全量
停机路径。协议与边界见
[Live Rank Split Go 产品面](LIVE-RANK-SPLIT-GO.zh-CN.md)。

## 3. 输出契约

- 默认输出人类可读摘要到 stdout；
- 进度输出到 stderr，不污染 JSON；
- `--json` 只输出一个 JSON object 到 stdout；
- `demo --json` 返回原始 demo 结果；
- `migrate --json` 至少包含：

```json
{
  "status": "success",
  "checkpoint": "/data/source.dcp",
  "output": "/data/source.dcp-migrated",
  "work_dir": "/data/source.dcp.shardferry",
  "topology": {
    "source_world_size": 4,
    "target_world_size": 3,
    "source_mesh_shape": [4],
    "target_mesh_shape": [3]
  },
  "plan": {"nodes": 4, "bytes": 128},
  "execution": {
    "bytes_moved": 128,
    "duration_seconds": 0.01,
    "retries": 0
  },
  "verification": {"passed": true, "method": "bitwise"}
}
```

退出码：

- `0`：成功；
- `2`：参数、路径、环境或迁移校验失败；
- `130`：人工中断；重跑同一 `migrate` 命令即可续跑。

## 4. 兼容层

以下命令仍可直接调用，供开发、自动化和现有脚本使用，但不保证产品级兼容：

```text
inspect plan apply verify proof-build proof-verify proof-merge
serve agent cutover-serve dcp-inspect dcp-export dcp-migrate
spec-http fetch-spec summarize
```

它们不出现在 `shardferry --help`。如果某个流程被用户真实使用，应优先把能力
提升为 `demo` / `doctor` / `migrate` 的一个选项，而不是继续扩张首屏命令面。

## 5. 产品不变量

1. 不在用户未指定时创建全局 checkpoint 副本。
2. 不修改 source checkpoint。
3. 未通过 bitwise 校验的数据不进入最终目标路径。
4. 输出目录、工作目录和源目录不重叠。
5. `--json` 不混入人类可读文本。
6. 中断后可通过重跑同一命令恢复，不要求用户手工编辑 journal。
