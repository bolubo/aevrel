# shardferry 交接卡

给帮忙复跑的同学：只需要按顺序执行，不要改源码，不要把 source checkpoint 删除或移动。

## 先跑本地 demo

在项目根目录，Windows PowerShell：

```powershell
.\scripts\run_demo.cmd --work-dir .\shardferry-demo
```

Linux：

```bash
./scripts/run_demo.sh --work-dir ./shardferry-demo
```

成功标志：输出里有 `Demo complete` 和 `verification: bitwise passed`。

也可以直接跑带 schema 检查的验收脚本：

```powershell
.\.venv\Scripts\python.exe scripts\acceptance.py --work-dir .\shardferry-acceptance
```

## 再跑 DCP 4->3（如果已有 checkpoint）

Windows：

```powershell
.\.venv\Scripts\python.exe -m shardferry `
  migrate --checkpoint "C:\path\to\source.dcp" --target-world 3
```

Linux：

```bash
.venv/bin/python -m shardferry \
  migrate --checkpoint /path/to/source.dcp --target-world 3
```

默认值：

- source world 自动推断
- placement 默认 `Shard(0)`
- work dir 是 `<source>.shardferry`
- 输出是 `<source>-migrated`

如果命令失败，不要删除 `.shardferry`；重新执行同一条命令会尝试续跑。

## 需要回传什么

请把以下内容发回来：

1. 执行的完整命令；
2. 终端最后 30 行输出；
3. `<source>.shardferry/report.json`（如果存在）；
4. 如果有报错，原样复制 traceback，不要只写“失败了”。

如果只是跑 demo，回传 `shardferry-demo/report.json`（或
`shardferry-acceptance/acceptance.json`）和终端输出即可。

## 独立验收的证据规则

优先回传原始 `report.json`、完整命令和终端输出。`scripts/acceptance.py` 只是
方便留档，不能单独替代原始证据。

不要向我索要“下一步该点哪里”；如果文档某一步不可执行，请原样记录卡在哪一行、
当时的完整报错和环境信息。这样得到的才是独立执行证据，而不是我边看边改出来的
结果。

## 不要做

- 不要修改 `source/` 下的文件。
- 不要删除 `.shardferry` 工作目录。
- 不要执行 `git init`、`git push` 或任何网络上传。
- 不要把 checkpoint 上传到公共网盘。

更完整的安装说明见 `QUICKSTART.zh-CN.md`。
