# Kueue strict guard E2E

这个目录记录 `0.2 Part 1` 的 CPU-only 验证场景。前置是已启用
`ConfigurablePreemptions` 的 Kueue 集群。

```bash
kubectl apply -f examples/kueue-adapter/kind-e2e/strict-setup.yaml
kubectl apply -f examples/kueue-adapter/preemption-config.yaml
kubectl apply -f examples/kueue-adapter/kind-e2e/strict-jobs.yaml

# 等两个 Workload 都 Admitted，并且 protected=1 / 0 出现
kubectl get workloads -o wide

kubectl apply -f examples/kueue-adapter/kind-e2e/preemptor-job.yaml
kubectl get workloads -o yaml
```

预期：

- `protected-train` 的 Workload 保持 `preemption-protected=1`，不出现
  `Preempted`；
- `eligible-train` 的 Workload 出现 `ConfigurablePreemption` / `Preempted`；
- `high-priority-preemptor` 获得 admission；
- Kueue 事件里能看到 `preemption config checkpoint-guard rule
  exclude-protected-workloads`。

注意：strict-setup 把 classical preemption 三个入口全部设为 `Never`，
并假设 FairSharing 未启用。这正是 configurable-only 路径成立的条件。

2026-10-22 的 CPU-only 实测结果：`protected-train` 保持 `protected=1` 并在
Pod 层继续 Running；`eligible-train` 的 Workload 出现 `Preempted`；没有
label 的 high-priority preemptor 没有成为 victim，并正常获得 quota。
