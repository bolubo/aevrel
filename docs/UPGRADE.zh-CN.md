# 升级与版本偏差

这份文档约束 Shardferry 的 chart、controller、agent、gateway 和 CRD 升级。
迁移过程中不要混跑不兼容版本。

## 版本关系

一个发布版本应满足：

- chart `version` 与 `appVersion` 一致；
- Helm values 的 controller/gateway/agent tag 等于 `appVersion`；
- controller 和 gateway 的 agent protocol 都是 `version: "1"`；
- `ShardferryCluster.status.version` 报告 gateway build version；
- CRD schema 与 controller 的 `shardferry.io/v1` 一致。

## 推荐升级顺序

1. 先升级管理集群的 controller/chart；
2. 再升级 source/target 的 gateway；
3. 最后升级 source/target agent；
4. 创建一条新的探测 migration 或运行 `preflight`；
5. 确认所有 `ShardferryCluster` ready 后再跑生产 migration。

不要在一个正在运行的 migration 中途替换 controller 或 target agent。先让它
结束，或 cancel 在 commit intent 之前的任务。

## CRD 升级

- chart 内的 CRD 由 Helm 安装/升级；不要手工删除 CRD；
- 新增字段必须保持旧 spec 可读；
- 删除字段、改类型或改语义必须提高 API version；
- 升级后先跑：

```bash
shardferryctl preflight -f migration.yaml --json
```

## 回滚

- 回滚 chart/controller/gateway 到上一个 release tag；
- 不要回滚 source checkpoint 或已提交 target；
- 如果已经进入 `commit-incomplete`，先 repair，不要用回滚掩盖状态；
- 回滚后保留 support bundle 和 migration report。

## 不兼容症状

- `VERSION_MISMATCH`：chart/agent/gateway 版本不一致；
- gateway `/v1/info` 失败：旧 gateway 不认识当前协议；
- preflight 能发现 rank、但迁移立即失败：controller 与 agent protocol 不一致；
- `ShardferryCluster` 长期不 ready：旧 gateway 或 endpoint/凭据问题。

出现上述情况，先停掉新 migration，不要继续重试。
