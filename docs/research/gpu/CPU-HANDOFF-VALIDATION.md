# CPU handoff target-job validation (2026-10-22)

状态：真实 kind CPU 集群验证通过；不代表 GPU 训练验证。

## 场景

- kind 集群：`sf-handoff-cpu`
- target Job：`batch/v1` Indexed Job
- target world size：2
- 运行器：`KubernetesTargetReady`
- Job 镜像：`busybox:1.36`

Job 的每个 Pod 通过 `JOB_COMPLETION_INDEX` 生成 rank，写入
`/dev/termination-log` 的 target report。

## 结果

- Job：`shardferry-handoff-cpu-step-200`
- 状态：`Complete`
- completions：`2/2`
- Pod 状态：两个 Pod 都是 `Succeeded`
- rank 0 / rank 1 的 report 都通过
  `schemaVersion/migrationUID/version/modelLoaded/optimizerLoaded/stepsRan`
  校验

这说明 CPU-only 下：

- Kubernetes Indexed Job 创建和等待链路可用；
- 每个 rank 的 termination message 可被读取；
- report 聚合不是只看 Job Complete；
- 任一 rank 缺失或 N-step 不足会导致失败。

## 复现

```bash
kind create cluster --name sf-handoff-cpu \
  --image kindest/node:v1.32.2
kind load docker-image busybox:1.36 --name sf-handoff-cpu

PYTHONPATH=src /root/lines/oss/aicluster/shardferry/.venv/bin/python \
  - <<'PY'
from shardferry.target_job import KubernetesTargetReady, TargetJobConfig
ready = KubernetesTargetReady(TargetJobConfig(
    template="examples/handoff/cpu-target-job.yaml",
    namespace="default",
    world_size=2,
    steps=10,
    job_id="handoff-cpu",
    checkpoint_template="/targets/{version_id}/rank{rank}",
    timeout=120,
    kubectl="scripts/kind-kubectl.sh",
))
ready("step-200")
PY
```

`scripts/kind-kubectl.sh` 是 kind 环境下的 `kubectl` 适配器；生产环境直接
使用本地 `kubectl`。

## 边界

- CPU/busybox reporter，不是真实模型训练；
- 2 个 rank，不是 4/8/128 rank；
- 没有 GPU、NCCL、DeviceMesh 或真实 K8s GPU target Job。
