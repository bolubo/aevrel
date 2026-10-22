# 跨集群 live acceptance

这个脚本用于已经有管理集群和 source/target gateway 的环境。它不会创建集群、
安装 release 或修改 source/target。

## 前置条件

- 管理集群已安装 controller 和 `ShardferryCluster`；
- source/target cluster 的 gateway 已部署且可访问；
- `shardferryctl` 在 PATH，`KUBECONFIG` 指向管理集群；
- `SHARDFERRY_AGENT_TOKEN` 已设置；
- migration 里有 `source.clusterRef` 和 `target.clusterRef`。

## 只做预检

```bash
python scripts/live_acceptance.py \
  --management-kubeconfig ~/.kube/management \
  --namespace shardferry-control \
  --migration examples/cross-cluster/migration.yaml \
  --output /tmp/live-acceptance.json
```

它会检查两个 `ShardferryCluster` 的 `status.ready`，再运行
`shardferryctl preflight --json`，并确认 `clusters.source/target` ready。

## 预检并执行

```bash
python scripts/live_acceptance.py \
  --management-kubeconfig ~/.kube/management \
  --namespace shardferry-control \
  --migration examples/cross-cluster/migration.yaml \
  --wait \
  --output /tmp/live-acceptance.json
```

`--wait` 会额外执行 `shardferryctl migrate --wait --json`。失败时报告会保留
命令、阶段和错误；不要把失败报告当成迁移成功。

## 不做什么

- 不自动创建 kind；
- 不自动安装 Helm；
- 不启动 target 训练 job；
- 不替代 GPU/FSDP 加载验证。
