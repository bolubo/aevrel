# K8s local-disk 部署骨架

这不是 Operator，只是 128 节点 local-disk 场景的 Job/StatefulSet 骨架。
它只负责启动 per-rank source agent 和 per-rank target writer，不负责自动发现
训练任务或执行 scheduler。

## Source

`source-service.yaml` 提供 headless Service，`source-statefulset.yaml` 给每个
source rank 一个 Pod 和一个本地 PVC。每个 Pod 只运行：

```text
serve --rank <ordinal> --listen 0.0.0.0:9000
```

`migration.yaml` 放在 ConfigMap `shardferry-source-spec` 中。每个 Pod 的
`/checkpoints` 放本 rank 的 local shard。

## Rank map

生成 client spec 前，先渲染 `rank_map`：

```bash
python scripts/render_rank_map.py \
  --service shardferry-source --namespace default \
  --world-size 128 --port 9000 --out rank-map.yaml
```

把 rank map 合入 `migration.yaml` 的 `source.mesh.rank_map`，再让 target 侧
`fetch-spec` 拉取。

## Target

`target-statefulset.yaml` 需要两个 ConfigMap：

- `shardferry-target-spec`：包含 `client.yaml` 和 `plan.json`；
- 每个 target Pod 自己的本地 PVC 作为 `/checkpoints`。

每个 Pod 运行：

```text
apply --spec /config/client.yaml --plan /config/plan.json \
  --target-rank <ordinal> --target-root /checkpoints
```

完成后 coordinator 收集每个 rank 的 report，并运行：

```bash
shardferry summarize \
  --reports-dir ./reports \
  --spec ./client.yaml \
  --plan ./plan.json \
  --out migration-summary.json
```

带上 `--spec` 和 `--plan` 时，`summarize` 是 fail-closed 的：缺少任一 target
rank、rank 重复、状态非 success、校验失败、chunk 数或计划字节数不符，都会
以 exit 2 失败。只做临时汇总时才省略这两个参数。

## 生产前还要补

- 真实的 local PV / hostPath / PVC 方案；
- 镜像构建和固定版本；
- coordinator 从各 target 节点收集 report 到同一目录（当前仓库只提供汇总与
  完整性校验）；
- 失败重试和 Job 重跑策略；
- target 一次性执行形态：当前 StatefulSet 的容器退出后会被 kubelet 重启，
  生产环境应换成 target agent 或带完成态控制的 Job/Operator；
- TLS/token 或仅限可信内网；
- 真实多节点 A/B 和 128-rank GPU 验证。
