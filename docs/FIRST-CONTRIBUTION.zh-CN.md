# Aevrel 首次贡献

这份文档只覆盖一条路径：先跑通现有真实 DCP 路径，再领一个边界清楚的
starter issue。预计 15–30 分钟。

## 1. 安装并跑通真实 DCP Demo

需要 Python 3.11+。Linux/macOS：

```bash
git clone https://github.com/bolubo/aevrel.git
cd aevrel
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
python -m aevrel demo --dcp --work-dir /tmp/aevrel-first-run --json
```

Windows PowerShell：

```powershell
git clone https://github.com/bolubo/aevrel.git
cd aevrel
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
python -m aevrel demo --dcp --work-dir "$env:TEMP\aevrel-first-run" --json
```

成功输出至少应包含：

```text
"verification": {"passed": true}
"source_readiness": {"status": "READY"}
"target_readiness": {"status": "READY"}
"target_load": "ok"
```

如果这一步失败，先不要领其他 issue；按 [SUPPORT](SUPPORT.zh-CN.md) 或
新 issue 报告失败命令和完整输出。

自动验收同一路径：

```bash
python scripts/first_run_acceptance.py
```

## 2. 选择一个 starter issue

当前活跃批次：

- [#11 restore-drill failure paths](https://github.com/bolubo/aevrel/issues/11)
- [#14 UI state fixtures](https://github.com/bolubo/aevrel/issues/14)
- [#20 target-load failure paths](https://github.com/bolubo/aevrel/issues/20)
- [#21 Helm optional components](https://github.com/bolubo/aevrel/issues/21)
- [#23 capability metadata source](https://github.com/bolubo/aevrel/issues/23)

这些 issue 默认是 **unassigned**。不要在认领前默认它已经被别人做。

## 3. 认领方式

在 issue 下评论：

```text
I'd like to take this. I plan to:
1. ...
2. ...
I will run: <validation command>
```

维护者确认后会把 issue assign 给你；如果你已经开始，也可以直接说明并
等待确认。一次只认领一个 starter issue。

## 4. 开发和验证

先看 [开发地图](DEVELOPMENT-MAP.zh-CN.md) 找到入口文件和对应对测试。

根据改动范围执行：

```bash
python -m pytest -q
go test ./...
python scripts/check_repo.py
python scripts/check_release.py
helm lint deploy/helm/aevrel
```

starter issue 至少应有一个明确的回归测试或文档验证命令。不要把未运行的
GPU、跨节点或真实 K8s 检查写成已验证。

## 5. 提交 PR

- PR 标题使用普通、描述性句子；
- 关联 issue；
- 写清 behavior change 和验证命令；
- 不修改 source checkpoint；
- 不把 secret、真实 checkpoint 或内部路径提交到仓库；
- 修改用户可见契约时同步更新文档和 CHANGELOG。

## 6. 维护者回看

维护者会优先检查：

- 是否只改了 issue 范围；
- 是否保护了 source-read-only 和 fail-closed 语义；
- 是否新增了可运行的验证；
- 是否有清晰的失败路径。
