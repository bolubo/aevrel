# 错误目录与下一步动作

这份目录面向用户和一线排障，不是内部异常清单。所有错误都应至少带上
`code`、`phase`、`rank`、`retryable`；用户看到错误后先按这里的动作处理。

## 输入与版本

| code | 用户看到的含义 | 下一步 |
|---|---|---|
| `INVALID_SPEC` | migration YAML 不合法 | 检查 worldSize、mesh、root、selector；先跑 `preflight --json` |
| `UNSUPPORTED_FORMAT` | 当前只支持 DCP | 不要尝试改格式字段；确认 checkpoint 是 PyTorch DCP |
| `VERSION_MISMATCH` | chart/controller/agent/gateway 版本不兼容 | 对齐 chart 和镜像 tag；查 compatibility matrix；不要混跑 |
| `PLAN_INVALID` | source/target 拓扑无法生成合法计划 | 核对 mesh、placement、world size、checkpoint metadata |

## source

| code | 用户看到的含义 | 下一步 |
|---|---|---|
| `SOURCE_UNREACHABLE` | gateway/agent 不可达 | 检查 `ShardferryCluster.endpoint`、Service、网络策略、token |
| `SOURCE_NOT_FOUND` | checkpoint root 或 shard 不存在 | 检查 hostPath、挂载、`.metadata`、pod 所在节点 |
| `INVENTORY_INCOMPLETE` | source rank 不全、重复或越界 | 查 agent selector、节点调度、`/v1/ranks`；不要手写 rankMap 掩盖问题 |
| `CHECKSUM_MISMATCH` | source 内容在迁移中变化或损坏 | 保留现场，重跑 preflight；不要删除 journal 或手工改 source |

## target

| code | 用户看到的含义 | 下一步 |
|---|---|---|
| `TARGET_CONFLICT` | target root 已有内容或 task 状态冲突 | 不要覆盖；换 root 或先确认旧 migration 状态 |
| `TARGET_LOCKED` | 另一个 task/migration 持有 target | 等锁释放或查旧任务；不要手工删锁 |
| `DISK_FULL` | target 空间不足 | 扩容/清理目标盘；不要重试同一 root |
| `PERMISSION_DENIED` | target 路径、token 或 RBAC 权限不足 | 检查 hostPath 权限、Secret、ServiceAccount、RBAC |
| `VERIFY_FAILED` | target 校验不通过 | 保留 staging/report；不要 commit 或手工复制文件；收集 support bundle |
| `COMMIT_INCOMPLETE` | commit 中途失败 | 只走 repair，不要重新做全量 transfer；先看 report 和 rank |

## target-ready

| code | 用户看到的含义 | 下一步 |
|---|---|---|
| `Incomplete` | target rank 报告缺失或重复 | 确认 Indexed Job 的 completions/parallelism 等于 worldSize；看 Pod 是否都 Succeeded |
| `LoadFailed` | 某 rank 模型或优化器加载失败 | 检查 DCP world size、模型代码、优化器状态和 `state_dict` 名称 |
| `StepFailed` | 某 rank 没跑够配置的 N step | 看训练日志；确认 validation.steps 与脚本实际步数一致 |
| `VersionMismatch` | rank 报告的 checkpoint version 不一致 | 检查 `SHARDFERRY_VERSION`、旧 Pod 报告和迁移版本 |
| `InvalidReport` | 报告 JSON 损坏、字段未知或 rank 冲突 | 用 `shardferry-target-report` 重新生成；不要手写不完整 JSON |
| `JobFailed` / `Timeout` | target validation Job 失败或超时 | 查看 Job/Pod 日志；确认镜像、GPU、挂载和 timeoutSeconds |
| `ProofPublishFailed` | target-ready proof 无法写 artifact store | 检查 controller PVC/目录和 artifact 服务；不要手工改 status 冒充成功 |

## network / control

| code | 用户看到的含义 | 下一步 |
|---|---|---|
| `NETWORK_TRANSIENT` | 短暂网络错误 | 可按 retryable 重试；检查 NAT、代理、gateway、重试预算 |
| `TIMEOUT` | 操作超时 | 先看具体 rank/phase；确认磁盘/网络是否卡住，再调 policy timeout |
| `CANCELLED` | 用户或系统取消 | 检查是否已跨过 commit intent；commit 后不能取消 |
| `INTERNAL` | 未分类内部错误 | 运行 support bundle；保留 migration UID、rank 和日志时间 |

## 排障顺序

1. 运行 `shardferryctl describe migration <name> -o json`；
2. 看 `phase`、`failedRanks`、`retryable`；
3. 运行 `preflight` 复现静态问题；
4. 收集 support bundle；
5. 只有确认是 transient 时才 retry；
6. `commit-incomplete` 只 repair，绝不重新全量传输。

## 绝对不要做

- 不要为了“重来一次”删除 source 或已提交 target；
- 不要在 commit 后手工移动/覆盖 target 文件；
- 不要把完整 kubeconfig、Secret 或未脱敏日志发到 issue；
- 不要把 timeout 当成失败原因，先看具体 phase 和 rank。
