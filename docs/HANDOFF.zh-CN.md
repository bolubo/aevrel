# Online checkpoint handoff（0.3）

状态：第一版实现中，先完成 warm target + bounded cutover。

## 用户任务

训练任务还在运行，checkpoint 以不可变版本目录产生。平台希望：

- target 在后台持续跟随最新完整 checkpoint；
- 发生抢占、计划迁移或扩容时，source 暂停时间有上限；
- 只传最后的变化量；
- target 启动并证明能继续训练；
- 失败时 source 恢复，target 不会留下混合版本。

这不是 CUDA context / Python 进程热迁移。0.3 迁移的是 checkpoint 状态，
不是运行中的 GPU 进程。

## 输入契约

source 必须产生完整版本目录：

```text
/checkpoints/job-a/
  step-100/
    .shardferry-complete
    rank0/...
    rank1/...
  step-200/
    .shardferry-complete
```

`.shardferry-complete` 是版本的完成标记。没有标记的目录不会进入 cutover；
已有标记的版本不应原地改写。

## 启动 warm handoff

`handoff` 是 `cutover-serve` 的产品入口。它默认在后台跟随 source 版本，
提供认证的 `status/follow/cutover/recover` HTTP API。

```bash
shardferry handoff \
  --source /source/job-a \
  --target /target/job-a \
  --listen 0.0.0.0:9450 \
  --token "$SHARDFERRY_CUTOVER_TOKEN" \
  --follow-interval 2 \
  --max-pause-seconds 60 \
  --pause-command 'python /opt/train/checkpoint_now.py --reason preemption' \
  --resume-command 'python /opt/train/resume.py' \
  --target-ready-command 'python /opt/train/target_ready.py --version {version_id}'
```

`--pause-command` 在最终 delta 前执行，必须等 checkpoint 完成并写出新的
`.shardferry-complete` 版本。`--resume-command` 在 cutover 失败、回滚或恢复时
执行。命令是 argv 模板，不做 shell 解释；`{version_id}` 会替换为选中的版本。

### 真实 target Job

给出一个 `batch/v1 Job` 模板即可让 promote 后自动启动 Indexed target Job：

```bash
shardferry handoff \
  --source /source/job-a \
  --target /target/job-a \
  --token "$SHARDFERRY_CUTOVER_TOKEN" \
  --target-job-template /opt/train/target-job.yaml \
  --target-job-namespace training \
  --target-job-id job-a \
  --target-world-size 2 \
  --validation-steps 10 \
  --target-checkpoint-template \
    '{target}/shadow/{version_id}/rank{rank}'
```

adapter 会：

- 强制 `completionMode=Indexed`，把 completions/parallelism 设为 target world；
- 给每个容器注入 checkpoint、version、world size、steps 和 report 环境变量；
- 用 `kubectl wait --for=condition=complete` 等 Job；
- 读取每个 Succeeded Pod 的 termination message；
- 校验所有 rank 的 `modelLoaded/optimizerLoaded/stepsRan/version`；
- 任一 rank 缺失、版本不一致或跑不够 N step，就让 cutover 失败并回滚。

Job 模板要求：

- 必须是 `batch/v1 Job`；
- 不要写死 `metadata.name`，或让名字随 `--target-job-id` 变化；
- 一个 target rank 对应一个 Pod；
- 训练脚本在模型、优化器和 N step 成功后调用 `shardferry-target-report`；
- 用户负责挂载 target checkpoint 路径和 GPU 资源。

## 触发 cutover

平台在抢占或计划迁移时调用：

```bash
curl -fsS -X POST http://handoff:9450/v1/cutover \
  -H "Authorization: Bearer $SHARDFERRY_CUTOVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

如果不指定 `version_id`，服务选择最新完整版本，并允许 pause 期间新写出的
最终 checkpoint 成为 promote 目标；显式指定 `version_id` 时，pause 期间出现
更新版本会 fail closed，不会被悄悄替换。服务会：

1. 拒绝非最新版本（默认策略）；
2. 检查 target lag 和 manifest；
3. 暂停 source；
4. 再做一次最终 delta；
5. promote；
6. 执行 target-ready hook；
7. 失败时 resume source 并回滚 target current。

## 当前验收

- 版本 marker / manifest / CAS / shadow / promote 语义已由现有 CDP 代码覆盖；
- warm target 后台跟随和 `/v1/status` lag 已有测试；
- pause/resume hook 的失败关闭和 target-ready 回滚已有测试；
- Kueue adapter 可通过 `shardferry.io/handoff: enabled` +
  `--handoff-url` 在 `Evicted` 后触发 cutover；
- Kubernetes target Job adapter 已接入：Indexed Job、per-rank termination
  report、全 rank 版本/加载/N-step 校验；
- 仍然缺少：
  - 真实 GPU target Job 的 N-step 训练验证；
  - 多集群 target placement 和自动目标选择。

## 非目标

- CRIU/cuda-checkpoint 透明进程恢复；
- source/target 任意 TP/PP 重排；
- dataloader/RNG/scheduler 的 L4 一致性；
- 在 source 还没写出完整版本时猜测可恢复点。
