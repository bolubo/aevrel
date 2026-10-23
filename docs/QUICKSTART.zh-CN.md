# Aevrel 新手操作手册

这份手册假设你只会复制命令，不需要改代码。

## 1. 先确认 Python

需要 Python 3.11 或更高版本。

Windows PowerShell：

```powershell
py -3 --version
```

Linux：

```bash
python3 --version
```

## 2. 安装项目

在项目根目录执行。Windows：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e .
```

Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e .
```

只有做真实 DCP 迁移时才需要 PyTorch：

```bash
# Linux CPU 机器
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch

# 或安装项目声明的 DCP extra（PyTorch 的安装源由平台决定）
.venv/bin/pip install -e ".[dcp]"
```

Windows 把 `.venv/bin/python` 换成 `.\.venv\Scripts\python.exe`。

## 2.1 构建 Kubernetes CLI

`aevrelctl` 是 Go 控制面的命令行入口：

```bash
go build -o bin/aevrelctl ./cmd/ctl
export PATH="$PWD/bin:$PATH"   # Linux/WSL
```

Kubernetes 用户路径还需要 kubeconfig。没有集群时，仍可用
`aevrelctl migrate -f FILE --dry-run` 离线校验并打印迁移 spec；
`preflight` 和 `plan` 需要能连接管理集群。

## 3. 先跑不需要任何数据的 demo

Windows：

```powershell
.\.venv\Scripts\python.exe -m aevrel demo
# 或双击/执行：scripts\run_demo.cmd
```

Linux：

```bash
.venv/bin/python -m aevrel demo
# 或：./scripts/run_demo.sh
```

看到类似下面的输出就是成功：

```text
Demo complete
  work dir:     /path/to/aevrel-demo
  target:       /path/to/aevrel-demo/target
  verification: bitwise passed
```

需要把结果交给脚本时，加 `--json`。

要继续体验真实 DCP、readiness、迁移和 target load，运行：

```bash
.venv/bin/python -m aevrel demo --dcp
```

它会在同一目录生成 `source.dcp` 和 `target-3.dcp`，并执行一次真实的
target DCP load。

默认会在当前目录生成 `aevrel-demo/`：

- `migration.yaml`：迁移规格
- `plan.json`：传输计划
- `report.json`：执行报告
- `target/w/rank0.bin` 等：目标 shard

不要修改 `source/` 下的文件；demo 会验证 source 没有被改动。

想看控制面而不想记命令，启动终端控制面：

```bash
.venv/bin/python -m aevrel tui
```

左侧是传输，中间是控制面，右侧是上下游关系；按底部提示操作，
`D` 生成 Demo，`I` 查询，`M` 迁移，`Q` 退出。


如果想把 schema 检查和 demo 结果一起留档：

```bash
.venv/bin/python scripts/acceptance.py --work-dir ./aevrel-acceptance
```

## 4. 迁移前检查

```bash
.venv/bin/python -m aevrel doctor \
  --checkpoint /data/job42/checkpoint
```

这会检查 Python、PyTorch/DCP、checkpoint metadata、源文件和输出路径，只读，
不会创建目标目录。看到 `Ready to migrate.` 再执行下一步。

## 4.1 查看 readiness

```bash
.venv/bin/python -m aevrel readiness \
  --checkpoint /data/job42/checkpoint \
  --target-world 3
```

这是只读检查，不会修改 source。`complete=true`、`fresh=true` 只说明
inventory 完整；没有真实 restore drill 时状态会是 `UNVERIFIED`，不能把
“有文件”当成“可恢复”。

需要验证 target 真的能继续训练时，提供一个目标训练命令：

```bash
.venv/bin/python -m aevrel restore-drill \
  --checkpoint /data/job42/checkpoint-3rank \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --steps 10
```

命令必须真实加载 model/optimizer、跑够 step，并写 target-ready report；
否则 drill 不会成功。

多 rank checkpoint 需要每个 rank 的报告。对每个 rank 运行一次：

```bash
.venv/bin/python -m aevrel restore-drill \
  --checkpoint /data/job42/checkpoint-3rank \
  --target-world 3 \
  --rank 0 \
  --target-command "python train_resume.py --checkpoint {checkpoint}" \
  --report ./reports/rank0.json
```

全部报告写好后，把它们接回 readiness：

```bash
.venv/bin/python -m aevrel readiness \
  --checkpoint /data/job42/checkpoint-3rank \
  --target-world 3 \
  --drill-report ./reports
```

只有报告覆盖 checkpoint 的所有 rank，且 model/optimizer 都加载成功，状态才会
从 `UNVERIFIED` 变成 `READY`。

## 5. 真实 DCP 4->3

假设源 checkpoint 目录是 `/data/job42/checkpoint`，执行：

```bash
.venv/bin/python -m aevrel \
  migrate \
  --checkpoint /data/job42/checkpoint \
  --target-world 3
```

默认结果：

- 工作目录：`/data/job42/checkpoint.aevrel`
- 目标 DCP：`/data/job42/checkpoint-migrated`
- 源目录只读，不会被修改
- 目标先写 staging，全部 bitwise 验证通过后才提交

如果目标目录已存在且非空，命令会拒绝覆盖；确认可以覆盖时加 `--force`。
中断后重新执行同一条命令会续跑，不要手工删除 `.aevrel` 目录。

迁移默认在 stderr 显示真实的 transfer 进度条（节点、字节、速率、ETA）；
`--json` 时不污染 stdout，自动化场景可额外加 `--no-progress`。

## 6. 2D mesh 怎么写

例如源 mesh 是 `2x2`，placement 是 `Shard(0),Shard(1)`；目标是 `3x1`：

```bash
.venv/bin/python -m aevrel \
  migrate \
  --checkpoint /data/job42/checkpoint \
  --source-mesh-shape 2,2 \
  --source-placements "Shard(0),Shard(1)" \
  --target-mesh-shape 3,1 \
  --target-placements "Shard(0),Replicate()" \
  --output /data/job42/checkpoint-3rank
```

`--target-mesh-shape 3,1` 会自动推出 target world size；只有显式传
`--target-world` 时才需要保证两者乘积一致。

不确定 mesh 时，先只用默认的 `Shard(0)` 做 1D FSDP 迁移；二维拓扑需要明确传入上面的参数。

## 7. 底层命令（开发/自动化）

产品入口是 `demo`、`doctor`、`capabilities`、`readiness`、
`restore-drill`、`migrate` 和 `state`。下面的命令仍然可用，但不保证稳定
兼容，也不会出现在 `aevrel --help`：

```bash
.venv/bin/python -m aevrel.cli dcp-inspect --input SOURCE_DCP
.venv/bin/python -m aevrel.cli plan --spec SPEC.yaml --out plan.json
.venv/bin/python -m aevrel.cli apply --spec SPEC.yaml --plan plan.json
.venv/bin/python -m aevrel.cli verify --spec SPEC.yaml --plan plan.json
```

`dcp-inspect` 会生成 `migration.yaml` 和抽取后的 raw source shard。它不修改原 DCP。

多 target rank 跑完后，把各 rank 的 `report.json` 放到同一目录，用 spec 和
plan 做完整性汇总：

```bash
.venv/bin/python -m aevrel.cli summarize \
  --reports-dir ./reports \
  --spec SPEC.yaml \
  --plan plan.json \
  --out summary.json
```

`--spec` 与 `--plan` 必须同时提供；这样会校验所有 target rank 是否齐全、状态
与 bitwise 校验是否通过、chunk 和计划字节数是否与 plan 一致。

## 8. HTTP 双机模式（后续）

源机器：

```bash
.venv/bin/python -m aevrel.cli serve \
  --manifest migration.yaml \
  --public
```

目标机器可以直接从源 agent 拉一份可用的 client spec，不需要手工复制或改 YAML：

```bash
.venv/bin/python -m aevrel.cli fetch-spec \
  --server http://SOURCE_IP:9000 \
  --target-storage ./target \
  --out client.yaml

.venv/bin/python -m aevrel.cli plan --spec client.yaml --out plan.json
.venv/bin/python -m aevrel.cli apply --spec client.yaml --plan plan.json
.venv/bin/python -m aevrel.cli verify --spec client.yaml --plan plan.json
```

如果网络暂时不可用，也可以把 `migration.yaml` 复制到目标机，然后用
`spec-http` 做同样的离线改写。

默认监听地址是 `127.0.0.1:9000`，只适合本机测试；跨机时加 `--public`
（等价于 `--listen 0.0.0.0:9000`），并放行防火墙端口。

客户端对 source agent 默认走直连，不继承 `HTTP_PROXY/HTTPS_PROXY`，避免
`NO_PROXY` 配错时把私网请求发到本地代理。

## 9. 高级与实验入口

这两个入口不属于最短路径，默认迁移不需要使用：

- `aevrel state capture|verify|restore`：高级入口，用于 rank-local
  resume state bundle；target-ready L4 需要在确认状态闭包时使用。
- `aevrelctl migrate ... --max-in-flight N`：live-rank-split 时一次
  同时在途的 rank 数；按网络和 target 容量设置，不能假设系统只迁一个。
- `aevrel-live-worker`：Go live 路径的按需 HTTP worker；当前是 CPU
  contract backend，启动后 controller 通过 `--live-url` 调用，GPU backend
  仍需后续验证。

实验入口失败时不得回退成“看起来成功”的静态迁移。先看
`docs/PRODUCT-FORM.zh-CN.md` 和 `docs/SUPPORT.zh-CN.md`。

## 10. 常见问题

### `torch` import 失败

确认当前命令使用的是项目的 venv，而不是系统 Python。重装 CPU 版：

```bash
.venv/bin/pip install --force-reinstall \
  --index-url https://download.pytorch.org/whl/cpu torch
```

### Windows 盘符路径

项目支持 `file:///C:/...` URI。路径有空格时给命令参数加双引号。

### WSL 的 `127.0.0.1` 不通

这是当前 WSL 环境的 loopback 特例。用 WSL/宿主机的实际网卡 IP，或先在普通 Linux/Windows 环境执行 demo。

### 出现失败时

不要删除 `.aevrel` 工作目录；它包含 journal 和已完成节点，重新执行同一命令会续跑。

物理双机实验需要单独的环境与证据记录；公开文档只保留可重复的 CPU 路径。
