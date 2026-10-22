# 排障与 support bundle

用户遇到问题时，不要让用户手工截图、翻十几个 Pod 日志。先收集一个
**脱敏支持包**，再定位。

## 1. 收集

在管理集群的 kubeconfig 上执行：

```bash
shardferry-support-bundle \
  --context management \
  --namespace shardferry-control \
  --output ./shardferry-support.tar.gz
```

它会收集：

- kubectl 客户端版本和当前 context；
- node、Pod、Deployment、DaemonSet、Service、PVC；
- `CheckpointMigration`、`ShardferryCluster`、events；
- 每个 Pod 最近 2000 行日志；
- Secret 的名字列表，不是 Secret 内容；
- helm release 列表。

默认会脱敏 `token`、`password`、`secret`、`authorization`、`bearer` 等字段。
不要用 `--no-redact`；这个脚本没有这个开关。

## 2. 先看什么

1. `manifest.json`：哪条命令失败、超时或缺少工具；
2. `commands/kubectl_checkpoint_migrations*`：migration phase、失败 rank；
3. `commands/kubectl_events*`：调度、镜像拉取、挂载和权限错误；
4. `commands/kubectl_shardferry_clusters*`：gateway 是否 ready；
5. `commands/logs_*`：agent/gateway/controller 的具体错误。

## 3. 常见错误方向

- **gateway 不可达**：endpoint、service type、网络策略、token；
- **source rank 缺失**：checkpoint hostPath、agent selector、node 调度；
- **target 写失败**：target root 权限、磁盘空间、hostPath 类型；
- **commit-incomplete**：不要重新传全量，走 repair；
- **target load 失败**：`.metadata`、rank root、dtype、world size。

## 4. 不要把什么发给别人

即使已经脱敏，也建议先检查：

- 内网地址、集群名、节点名；
- 业务 namespace、checkpoint 路径；
- Pod 日志中的模型名或数据路径。

如果必须提供未脱敏信息，只发最小片段，不要发完整 kubeconfig、Secret 或
完整日志目录。

## 5. 用户自助链路

```bash
shardferryctl preflight -f migration.yaml --json
shardferryctl describe migration <name> -o json
shardferryctl logs migration <name>
shardferry-support-bundle --namespace shardferry-control
shardferry-target-load --checkpoint /targets/... --rank 0
```

目标是让用户在发 issue 前就能排除：没装好、认证错、rank 缺失、target
空间不足、target DCP 不可读这几类问题。
