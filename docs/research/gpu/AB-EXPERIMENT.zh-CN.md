# shardferry 物理 A/B 实验卡

## 目的

验证 `shardferry` 在**两台独立机器、两份独立磁盘、无共享文件系统**下的迁移：

- A 机器只持有 source checkpoint；
- B 机器只通过 HTTP 拉取必要的 source shard；
- B 在本机完成 target 写入和 bitwise 校验；
- 迁移前后 A 的 source 内容不变。

这不是“同一台机器开两个进程”。同一台机器的本地双进程测试只算前置验证。

## 推荐拓扑

```text
A（source）                           B（target）
source.dcp                            client.yaml
source-work/migration.yaml             target/...
shardferry serve --public  <----TCP 9000----  shardferry fetch-spec/plan/apply/verify
```

源和目标可以是 Linux 或 Windows；两端使用同一个冻结交接包的 SHA256。

## 前置检查

- A/B 都能运行 Python 3.11+；B 至少安装基础依赖，若要导出目标 DCP 再安装 `.[dcp]`。
- B 能访问 A 的 TCP 9000 端口，A/B 没有共享目录。
- 使用固定交接包，不从不同版本混用代码。
- A 的 source checkpoint 只读；先记录 source 文件树 SHA256。
- 目标 world size、mesh shape、placements 明确写在实验记录中。

## 正常路径

### A 机器

```bash
# 在 A 的交接包目录
python -m shardferry.cli dcp-inspect \
  --input /data/source.dcp \
  --work-dir /data/source-work \
  --target-world 3

python -m shardferry.cli serve \
  --manifest /data/source-work/migration.yaml \
  --public
```

`serve` 默认只监听本机；`--public` 显式绑定 `0.0.0.0:9000`。如果 A 是 WSL，
还要把 Windows 主机端口映射到 WSL（见文末）。

### B 机器

```bash
# 解压交接包，创建干净 venv
python -m shardferry.cli fetch-spec \
  --server http://A的局域网IP:9000 \
  --target-storage ./target \
  --out client.yaml

python -m shardferry.cli plan --spec client.yaml --out plan.json
python -m shardferry.cli apply --spec client.yaml --plan plan.json \
  --report report.json
python -m shardferry.cli verify --spec client.yaml --plan plan.json
```

B 不需要 source DCP，也不需要手工编辑 source URI。

如果要把 target 转成 DCP：

```bash
python -m shardferry.cli dcp-export \
  --spec client.yaml \
  --output ./target.dcp
```

## 多 source rank / local-disk 模式

当 source shard 分布在多个节点时，每个 source rank 只启动自己的 agent，不要求
一个节点能看到所有盘：

```bash
# 在 rank r 的节点上，port_r 是该节点的端口
shardferry serve \
  --manifest source-work/migration.yaml \
  --rank r \
  --listen 0.0.0.0:port_r
```

`migration.yaml` 的 `source.mesh.rank_map` 要写成 rank → endpoint：

```yaml
rank_map:
  - rank: 0
    endpoint: http://source-node-0:9100
  - rank: 1
    endpoint: http://source-node-1:9100
```

目标节点也不要由一个进程写所有盘；每个 target rank 在本节点执行：

```bash
shardferry apply \
  --spec client.yaml --plan plan.json \
  --target-rank r --target-root /local/checkpoints/target

shardferry verify \
  --spec client.yaml --plan plan.json \
  --target-rank r --target-root /local/checkpoints/target
```

`target-root` 是该节点自己的本地目录；coordinator 只负责生成 plan 和汇总
per-rank report。这个模式是 128 卡/多节点 local-disk 场景的前置形态。

所有 target rank 的 report 收齐后，用 spec 和 plan 做完整性验收：

```bash
shardferry summarize \
  --reports-dir ./reports \
  --spec client.yaml \
  --plan plan.json \
  --out migration-summary.json
```

缺少 rank、重复 rank、失败状态、校验失败、chunk/字节数不符都会让命令 exit 2；
不要把只做加总的输出当成 coordinator 验收结果。

没有多机时可以先跑 CPU 数据面仿真：

```bash
python scripts/rank_smoke.py \
  --source-world 128 --target-world 64 \
  --work-dir /tmp/shardferry-rank-smoke
```

它会启动 128 个本地 source agent，并通过 HTTP 完成 128→64 的 target 写入和
bitwise 校验；这只能证明数据面，不替代真实多节点测试。

## 必须采集的数据

1. A/B 的 OS、Python、`torch` 版本（如果安装）；
2. `plan.json` 的 `total_bytes`、`nodes`、`full_gather`；
3. A 的 `/metrics` 前后值；
4. B 的 `report.json`；
5. B 的 `verify` 输出；
6. A source 文件树迁移前后 SHA256；
7. 完整命令和失败 traceback（如果有）。

正常路径的通过标准：

- `verify` 为 `passed: true`；
- target 文件大小符合 target mesh；
- `/metrics.bytes_served` 与计划传输字节一致（或能解释验证阶段额外读取）；
- `full_gather` 为 false；
- source 文件树 SHA256 不变。

## 故障场景

按顺序做，每个场景使用独立的临时 B target 目录：

1. **正常 4→3**：按上面的正常路径跑一遍。
2. **中断续跑**：B 的 `apply` 进行到约一半时停止；不要删除 `.shardferry`，
   重新执行同一 `apply`，然后 `verify`。
3. **A 网络中断**：`apply` 过程中停止 A 的 `serve`，确认 B 报出清晰网络错误；
   重启 A 的 `serve` 后重跑同一 `apply`。
4. **source checksum 错误**：只在可丢弃的测试副本上改一个 source byte；
   预期在 target 写入前失败，source 不应被修改。
5. **target 写失败**：在可丢弃的 B 目录上注入写失败；预期报错并保留 journal，
   不应提交最终 target。

不要对真实训练 checkpoint 做破坏性故障注入。

## 交接纪律

- 只把固定 tar/zip 包和 `SHA256SUMS` 给同学；
- 同学按包内文档独立执行，执行过程中不追加“下一步提示”；
- 回传原始命令输出和 `report.json`，不只回传自写的成功摘要；
- 如果失败，保留 `.shardferry` 和 traceback，再分析。

## 一次跑完的时间顺序

以下假设：A 是当前机器，B 是同学机器；A 和 B 不共享目录；使用 4→3 的
source DCP。小 checkpoint 全流程通常 5-10 分钟，大 checkpoint 主要耗在
`apply/verify`。

| 阶段 | A 机器 | B 机器 | 产出 |
|---|---|---|---|
| T0 准备 | 确认 source DCP、包 hash、`target-world=3` | 解压包、建干净 venv | A/B 环境信息 |
| T1 建 source work | `dcp-inspect` 生成 `source-work` | 等 A 给出 URL | `migration.yaml` |
| T2 启动 agent | `serve --public` | 记录 A IP；不要复制 source DCP | A 的 serve 日志 |
| T3 正常迁移 | 观察 `/metrics` | `fetch-spec -> plan -> apply -> verify` | `client.yaml/plan/report` |
| T4 中断续跑 | 在 B apply 中停 A agent，再重启 | 同一 spec 重跑 apply，再 verify | journal、恢复后的 report |
| T5 源校验失败 | 用可丢弃的 source-work 副本改一个 byte | 重新 fetch spec；apply 应在写 target 前失败 | 失败 traceback |
| T6 target 写失败 | 不动 | 在可丢弃 target 制造写入冲突 | 失败 traceback、journal |
| T7 收尾 | 停 server，记录 source hash | 保留 report、目标文件、日志 | 原始证据 |

T3 的具体命令：

```bash
# B
python -m shardferry.cli fetch-spec \
  --server http://A的LAN_IP:9000 \
  --target-storage ./target \
  --out client.yaml
python -m shardferry.cli plan --spec client.yaml --out plan.json
python -m shardferry.cli apply --spec client.yaml --plan plan.json \
  --report report.json
python -m shardferry.cli verify --spec client.yaml --plan plan.json
```

T4 的中断续跑：

1. B 执行 `apply`，看到 journal 产生后，在 A 的终端按 `Ctrl-C` 停止 `serve`；
2. B 应报出清晰的连接错误，target 不应被标记完成；
3. A 重启同一条 `serve --public`；
4. B 不再重新 `fetch-spec`，直接重跑同一 `apply` 和 `verify`；
5. 记录 journal 中的已完成节点、`report.json` 的 `retries` 和最终 verify。

T5 只能在 `source-work` 的副本上做，绝不能改真实 checkpoint。

T5 的具体操作（A 端）：

```bash
cp -a source-work source-work-bad
python - <<'PY'
from pathlib import Path
p = Path("source-work-bad/source/w/rank0.bin")
data = bytearray(p.read_bytes())
data[0] ^= 0xFF
p.write_bytes(data)
PY
# 停掉正常 server 后，用同一端口启动坏副本
shardferry serve --manifest source-work-bad/migration.yaml --public
```

B 端重新执行 `fetch-spec`、`plan`、`apply`。`apply` 应在 source checksum
预检阶段失败，真正的 `source-work` 不能被改动。

T6 的具体操作（B 端）：

A 端先确认已经恢复为正常 source：

```bash
shardferry serve --manifest source-work/migration.yaml --public
```

B 端：

```bash
mkdir -p bad-target
printf 'block the tensor directory\n' > bad-target/w
shardferry fetch-spec \
  --server http://A的LAN_IP:9000 \
  --target-storage ./bad-target \
  --out bad-client.yaml
shardferry plan --spec bad-client.yaml --out bad-plan.json
shardferry apply --spec bad-client.yaml --plan bad-plan.json
```

预期：文件系统冲突导致清晰失败，不能留下已提交的最终 target；失败现场和
journal 保留供分析。

## 给 B 机器 Agent 的提示词

把下面这段原样给同学机器上的 Agent，不附加我们聊天记录：

```text
你是 shardferry 物理 A/B 实验的 B 端执行者。只使用当前解压包和
docs/AB-EXPERIMENT.zh-CN.md，不要读取 A 的 source DCP，不要修改源码。
按 T3 执行 fetch-spec、plan、apply、verify，并记录完整命令、终端输出、
report.json、client.yaml、plan.json。遇到错误立即停止，不要自行修复；
保留 .shardferry 和 target 现场。不要上传 checkpoint，不要 git push。
```

## WSL 额外注意

如果 A 的 server 跑在 WSL，B 在另一台机器，B 看不到 WSL 的内部 IP。需要在
Windows 主机做端口转发，例如（管理员 PowerShell，执行前确认目标 IP）：

```powershell
netsh interface portproxy add v4tov4 `
  listenaddress=0.0.0.0 listenport=9000 `
  connectaddress=<WSL_IP> connectport=9000
```

并在 Windows 防火墙放行 TCP 9000。测试结束后删除对应 portproxy 规则。
