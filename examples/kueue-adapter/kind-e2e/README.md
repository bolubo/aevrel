# Kueue strict guard end-to-end example

This directory contains a CPU-only verification scenario. The prerequisite is
a Kueue cluster with `ConfigurablePreemptions` enabled.

```bash
kubectl apply -f examples/kueue-adapter/kind-e2e/strict-setup.yaml
kubectl apply -f examples/kueue-adapter/preemption-config.yaml
kubectl apply -f examples/kueue-adapter/kind-e2e/strict-jobs.yaml

# Wait until both Workloads are Admitted and protected=1 / 0 appears.
kubectl get workloads -o wide

kubectl apply -f examples/kueue-adapter/kind-e2e/preemptor-job.yaml
kubectl get workloads -o yaml
```

Expected result:

- `protected-train` keeps `preemption-protected=1` and is not `Preempted`;
- `eligible-train` receives `ConfigurablePreemption` / `Preempted`;
- `high-priority-preemptor` is admitted;
- Kueue events include `preemption config checkpoint-guard rule
  exclude-protected-workloads`.

`strict-setup` sets all three classical preemption entry points to `Never` and
assumes FairSharing is disabled. Those conditions make the configurable-only
path valid.

The CPU-only run on 2026-10-22 left `protected-train` at `protected=1` with its
pods still Running, emitted `Preempted` for `eligible-train`, and admitted the
high-priority preemptor that had no protection label.
