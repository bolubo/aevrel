# Kueue preemption guard example

This directory contains the minimal integration package. It publishes
checkpoint protection state as Workload labels and does not modify Kueue
source code. Prefer the Helm path for a production installation:

```bash
helm upgrade --install aevrel deploy/helm/aevrel \
  --set kueueAdapter.enabled=true \
  --set kueueAdapter.strict=true \
  --set kueueAdapter.createPreemptionConfig=true
```

With the default `observation-source=auto`, the adapter associates a
`CheckpointRecord` first and reads Job annotations only when no record exists.

## Prerequisites

- a Kubernetes cluster;
- Kueue with `ConfigurablePreemptions` enabled;
- an existing LocalQueue and ClusterQueue;
- training Jobs that can write `aevrel.dev/*` annotations.

## 1. Build and load the image

```bash
docker build \
  -f deploy/docker/Dockerfile.kueue-adapter \
  -t aevrel-kueue-adapter:dev .
kind load docker-image aevrel-kueue-adapter:dev
```

## 2. Install RBAC and the adapter

```bash
kubectl apply -f examples/kueue-adapter/rbac.yaml
kubectl apply -f examples/kueue-adapter/deployment.yaml
kubectl -n aevrel-system rollout status deploy/aevrel-kueue-adapter
```

The example Deployment uses `--strict=true`. Before starting, it requires the
`checkpoint-guard` configuration to exist, all three classical ClusterQueue
preemption entry points to be `Never`, and the numeric-label rule to use
`maxValue=0` and `fallbackValue=1`. Validation failure exits the process
instead of allowing a state that appears protected but may still be preempted.

## 3. Apply the candidate filter rule

```bash
kubectl apply -f examples/kueue-adapter/preemption-config.yaml
```

This is the strict profile with `fallbackValue: 1`: Workloads that do not
explicitly set `preemption-protected=0` are excluded from preemption
candidates. Only an explicitly eligible Workload may be preempted. For a
trial or fail-open rollout, use `preemption-config-advisory.yaml`, but do not
treat it as binary protection.

Point the target ClusterQueue at the configuration:

```bash
kubectl annotate clusterqueue <name> \
  kueue.x-k8s.io/preemption-config-name=checkpoint-guard
```

## 4. Submit a training Job

Replace the queue name, image, and command in `job-publisher.yaml` with real
values:

```bash
kubectl apply -f examples/kueue-adapter/job-publisher.yaml
kubectl get workloads.kueue.x-k8s.io -w
```

The Workload should receive:

```text
aevrel.dev/preemption-protected=1
aevrel.dev/preemption-reason=ProgressThreshold
aevrel.dev/training-progress-percent=85
aevrel.dev/checkpoint-complete=1
```

Inspect the control-plane view:

```bash
aevrelctl kueue status
aevrelctl kueue explain training
```

## Validation boundary

- This example proves that Kueue sees the filter label; it does not prove that
  protection is absolute.
- Check the ClusterQueue's classical preemption settings so they cannot bypass
  the filter rule.
- The strict profile also requires FairSharing not to participate in
  preemption.
- Real preemption validation must inspect Kueue events and the selected
  victim, not only labels.
