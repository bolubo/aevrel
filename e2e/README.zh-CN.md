# shardferry E2E harness

`e2e/run.py` 是控制器/agent 的本地故障矩阵：

- `baseline`：正常迁移；
- `target-fail`：target execute 失败一次后重试；
- `target-kill`：传输中杀掉 target，再用同一 state dir 启动；
- `controller-restart`：迁移中重启本地 controller。

## 运行环境

- 需要 `kind`、`kubectl`、`go`；
- Python 使用 **3.12**。当前这台 WSL 的 `/usr/bin/python3` 是 3.14，
  `ThreadingHTTPServer` 在 loopback 上会 accept 后不返回响应，导致 harness
  误以为 agent 卡住；
- WSL NAT 下不要用 `127.0.0.1` 互访；使用 `hostname -I` 的第一个地址，
  并让 agent/controller 绑定 `0.0.0.0`。

```bash
python3.12 -m venv /tmp/shardferry-py312
/tmp/shardferry-py312/bin/pip install numpy PyYAML jsonschema
```

## 运行

```bash
cd /root/lines/oss/aicluster/shardferry
host_ip=$(hostname -I | awk '{print $1}')
/tmp/shardferry-py312/bin/python e2e/run.py \
  --scenario all \
  --cluster-name shardferry-e2e \
  --host-ip "$host_ip" \
  --keep
```

默认规模是 16 source ranks → 8 target ranks、每 rank 64KB。harness 会复用
已存在的 kind 集群，并为每个 scenario 使用独立 target root；结束后可执行
`kind delete cluster --name shardferry-e2e` 清理。
