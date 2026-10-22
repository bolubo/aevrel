# Target-ready：证明目标端真的能继续训练

状态：0.2 第三部分实现契约（2026-10-22）

## 它解决什么问题

“checkpoint 文件已经搬到 target”只证明字节到了，不证明训练能恢复。
target 可能因为 DCP metadata、world size、模型代码、优化器状态或数据路径
不匹配而在第一步崩溃。

shardferry 的 `target-ready` 结论必须满足：

1. 每个 target rank 都加载了模型；
2. 每个 target rank 都加载了优化器；
3. 每个 target rank 至少运行 N 个训练 step；
4. 所有 rank 报告同一个 checkpoint version；
5. Job 最终成功，报告完整且没有冲突。

只有以上全部成立，migration 才会进入 `succeeded`，并设置
`TargetReady=True`。文件迁移完成但 target 失败时，migration 进入
`target-failed`；已经提交的 target 文件不会被伪装成“整体成功”。

## 0.2 的等级边界

- **L3 是 0.2 承诺**：模型 + 优化器加载，真实训练 N step；
- **L4 不在 0.2 承诺内**：dataloader、RNG、scheduler 的逐 step 连续性。

报告里的 `stateComplete=false` 表示 L4 状态没有被证明；它不会阻止 L3，
但会在 status 和 proof 中明确暴露，禁止把 L3 误报成完整训练状态恢复。

## 工作方式

1. source → target 的数据迁移按原有事务提交；
2. controller 创建一个 Indexed Kubernetes Job，或观察用户已有的 Job；
3. controller 给每个容器注入：
   - `SHARDFERRY_MIGRATION_UID`
   - `SHARDFERRY_VERSION`
   - `SHARDFERRY_TARGET_WORLD_SIZE`
   - `SHARDFERRY_VALIDATION_STEPS`
   - `SHARDFERRY_CHECKPOINT_TEMPLATE`
   - `SHARDFERRY_REPORT_PATH`
4. 每个 rank 的真实训练代码在加载和 N step 成功后调用
   `shardferry-target-report`；
5. report 写入容器的 termination message；
6. controller 从成功 Pod 的 termination message 读取 JSON，聚合所有 rank；
7. 只有 Job Complete + 全 rank 报告通过，才写 `TargetReady=True` 和 proof。

使用 termination message 是有意的：不需要共享盘、不增加回调端口、不要求
target Job 能访问 controller。控制器重启后仍可从 Job/Pod 状态恢复。

## 最小报告

安装 shardferry Python 包后，训练脚本只需在最后调用：

```python
from shardferry.target_ready import write_target_ready_report

write_target_ready_report(
    model_loaded=True,
    optimizer_loaded=True,
    steps_ran=10,
    state_complete=False,  # L4 未保存时保持 false
)
```

也可以使用 CLI：

```bash
shardferry-target-report \
  --model-loaded \
  --optimizer-loaded \
  --steps-ran "$SHARDFERRY_VALIDATION_STEPS"
```

`--model-loaded` / `--optimizer-loaded` 只能在代码确实完成加载后传；
训练脚本加载失败应直接非零退出，不能继续写成功报告。
目标训练镜像必须安装 shardferry wheel（或把同样的 report helper 打进镜像），
否则 `shardferry-target-report` 不可用。

## Migration 配置

```yaml
target:
  worldSize: 2
  mesh:
    shape: [2]
    placements: ["Shard(0)"]
  rootTemplate: /targets/rank{rank}
  validation:
    version: step-100000
    steps: 10
    timeoutSeconds: 900
    jobTemplate:
      apiVersion: batch/v1
      kind: Job
      spec:
        template:
          spec:
            restartPolicy: Never
            containers:
              - name: trainer
                image: ghcr.io/example/trainer:latest
                command: ["/bin/sh", "-c"]
                args:
                  - |
                    set -eu
                    rank="${JOB_COMPLETION_INDEX:?}"
                    checkpoint="${SHARDFERRY_CHECKPOINT_TEMPLATE//\{rank\}/$rank}"
                    python train_resume.py \
                      --checkpoint "$checkpoint" \
                      --steps "$SHARDFERRY_VALIDATION_STEPS"
                    shardferry-target-report \
                      --model-loaded \
                      --optimizer-loaded \
                      --steps-ran "$SHARDFERRY_VALIDATION_STEPS"
```

完整可复制版本见
[`examples/target-ready/migration.yaml`](../examples/target-ready/migration.yaml)。

### 独立 RestoreDrill

不经过 migration 也可以直接验证一个 `CheckpointRecord`：

```bash
kubectl apply -f examples/checkpoint-control/restore-drill.yaml
kubectl get sfdrill training-step-100 -w
shardferryctl readiness training-step-100
```

`RestoreDrill.spec.jobTemplate` 与 migration 的 target validation 使用同一套
per-rank report 契约。也可以设置 `spec.jobName` 观测一个已有 Job；两条路径
必须二选一。drill 成功后会把 load proof 绑定到 checkpoint version 与
metadata digest；checkpoint 变化后旧 proof 自动失效。

### Job 模板约束

- 必须是 `batch/v1 Job`；
- **0.2 MVP 假定每个 target rank 一个 Pod**，controller 会设置为
  Indexed completion；多 GPU/Pod 的 torchrun 形态暂不支持；
- `completions` / `parallelism` 必须等于 `target.worldSize`，或留空由
  controller 填充；
- 模板必须把每个 rank 的 target 本地目录挂载到容器里；
- 报告路径由 controller 注入，用户代码不要自己覆盖
  `SHARDFERRY_REPORT_PATH`；
- 0.2 只支持 controller 所在 target 集群中的 Job。target 在远端集群时，
  应由 target 侧控制面创建 migration 或使用后续的远端 launcher adapter。

也可以不创建 Job，而用 `validation.jobName` 观察一个已经存在的 Job。
该 Job 必须带 `shardferry.io/migration-uid=<migration UID>` 和
`shardferry.io/target-version=<version>` annotation；
建议由 controller 创建模板，避免报告串线。

## Status 与 proof

成功：

```yaml
status:
  phase: succeeded
  targetReady:
    ready: true
    condition: Ready
    version: step-100000
    ranksReady: 2
    ranksTotal: 2
    stepsRan: 10
    stateComplete: false
    durationMilliseconds: 42500
    proofRef: http://controller:8082/v1/artifacts/sha256:...
```

失败时 `phase=target-failed`，`TargetReady=False`，例如：

- `Incomplete`：缺少 rank 报告；
- `VersionMismatch`：rank 报告的 version 不一致；
- `LoadFailed`：模型或优化器未加载；
- `StepFailed`：没有跑够 N step；
- `JobFailed` 或 `Timeout`：目标 Job 失败/超时；
- `InvalidReport`：JSON 损坏、字段未知或 rank 冲突；
- `ProofPublishFailed`：proof 无法写入 artifact store。

无论哪种失败，`data committed` 和 `target-ready` 是两个独立事实；
不会出现“文件已搬完所以 migration succeeded”的误报。

## 当前限制

- 0.2 只实现通用 Job adapter，Kubeflow TrainJob adapter 是后续薄适配；
- 不解析用户训练框架的 optimizer 格式，框架必须自己完成真实 load；
- 不做 dataloader/RNG/scheduler 的 L4 证明；
- 不支持 target Job 在远端 Kubernetes 集群、而 controller 在另一个集群
  的场景；
- 不把 target-ready 当作训练性能 benchmark；它只证明可继续运行。
