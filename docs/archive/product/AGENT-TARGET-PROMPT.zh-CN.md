# Target B Agent 任务卡

你只负责 B（target）端。A 端 source checkpoint、source work 和 `.shardferry`
现场都不属于你。

## 输入

- 一个解压后的 shardferry 交接包；
- A 端给出的 HTTP 地址，例如 `http://192.168.1.20:9000`；
- B 端本地目标目录，例如 `./target`。

不要读取 A 的 source DCP，不要修改源码，不要访问聊天记录或内部 WORKLOG。

## 正常任务

在交接包根目录执行：

```bash
python -m shardferry.cli fetch-spec \
  --server http://A的IP:9000 \
  --target-storage ./target \
  --out client.yaml

python -m shardferry.cli plan --spec client.yaml --out plan.json
python -m shardferry.cli apply --spec client.yaml --plan plan.json \
  --report report.json
python -m shardferry.cli verify --spec client.yaml --plan plan.json
```

成功时保存并回传：

- 完整命令；
- 终端输出；
- `client.yaml`、`plan.json`、`report.json`；
- `verify` 输出；
- OS、Python 版本。

## 失败时

不要删除 `.shardferry`、`target` 或日志。停止执行，回传：

- 卡住的命令；
- 完整 traceback；
- `client.yaml` 和 journal；
- 当前目录结构。

如果 `/health` 或 `fetch-spec` 连不上，先等待 30 秒并用同一命令重试一次；
仍失败就报告网络地址/端口不可达，不要自行修改 A 的 source。

## 禁止

- 不要上传 checkpoint 或 target；
- 不要执行 `git init`、`git push`；
- 不要用自己写的替代测试替换上述命令；
- 不要根据“看起来应该能过”修改代码。
