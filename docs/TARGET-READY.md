# Target-Ready Validation

Target-ready answers a narrower question than migration:

> Can the target ranks load the model and optimizer and run real training
> steps?

A target is ready only when every expected rank reports:

- the model loaded;
- the optimizer loaded;
- the requested number of steps ran;
- the checkpoint version matches;
- the report is complete and valid.

If these conditions are not met, the migration does not become `succeeded`.

## Evidence levels

- **L3**: model and optimizer load and the target runs real training steps.
- **L4**: L3 plus continuity evidence for dataloader, RNG, scheduler, and
  rank-local state.

L4 is not implied by a successful L3 check. `stateComplete=false` must remain
visible and must not be presented as complete state restoration.

## Report

After installing the Aevrel Python package, the target training script can
write a report:

```python
from aevrel.target_ready import write_target_ready_report

write_target_ready_report(
    model_loaded=True,
    optimizer_loaded=True,
    steps_ran=10,
    state_complete=False,
)
```

Or use the CLI:

```bash
aevrel-target-report \
  --model-loaded \
  --optimizer-loaded \
  --steps-ran "$AEVREL_VALIDATION_STEPS"
```

Pass `--model-loaded` and `--optimizer-loaded` only after the target code has
actually loaded them. A failed load must exit non-zero and must not write a
successful report.

The target image must include the Aevrel package or an equivalent report
helper.

## Kubernetes Job

The controller can create an Indexed Job or observe an existing Job. The
template must use `batch/v1` and mount the target checkpoint data for each
rank.

The controller injects:

```text
AEVREL_MIGRATION_UID
AEVREL_VERSION
AEVREL_TARGET_WORLD_SIZE
AEVREL_VALIDATION_STEPS
AEVREL_CHECKPOINT_TEMPLATE
AEVREL_REPORT_PATH
```

Minimal container command:

```bash
set -eu
rank="${JOB_COMPLETION_INDEX:?}"
checkpoint="${AEVREL_CHECKPOINT_TEMPLATE//\{rank\}/$rank}"
python train_resume.py \
  --checkpoint "$checkpoint" \
  --steps "$AEVREL_VALIDATION_STEPS"
aevrel-target-report \
  --model-loaded \
  --optimizer-loaded \
  --steps-ran "$AEVREL_VALIDATION_STEPS"
```

The current contract assumes one Pod per target rank. A multi-GPU torchrun
layout inside one Pod is not supported by this contract yet.

## Status and failures

Success sets `TargetReady=True` and writes a proof reference. Failure conditions
include:

- `Incomplete`: one or more expected rank reports are missing;
- `VersionMismatch`: reports refer to different checkpoint versions;
- `LoadFailed`: model or optimizer load failed;
- `StepFailed`: fewer than the requested steps ran;
- `JobFailed` or `Timeout`: the target Job failed or timed out;
- `InvalidReport`: malformed JSON, unknown fields, or duplicate ranks;
- `ProofPublishFailed`: the proof could not be stored.

Data movement and target validation are separate facts. A target can contain
files and still be unusable for training.

## RestoreDrill

`RestoreDrill` can validate a checkpoint without creating a migration:

```bash
kubectl apply -f examples/checkpoint-control/restore-drill.yaml
kubectl get sfdrill training-step-100 -w
aevrelctl readiness training-step-100
```

Set either `spec.jobTemplate` or `spec.jobName`, not both. A successful drill
binds the load proof to the checkpoint version and metadata digest; changing
the checkpoint invalidates the old proof.

## Limitations

- The target must run in the controller's cluster in the current contract.
- Aevrel does not parse framework-specific optimizer formats.
- L4 state continuity is not inferred from an L3 result.
- Target-ready is not a training performance benchmark.
