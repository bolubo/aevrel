# Target-Ready Validation

Target-ready answers a narrower question than migration:

> Can the target ranks load the model and optimizer, run real training steps,
> and prove what resume state they restored?

A target is ready only when every expected rank reports:

- the model loaded;
- the optimizer loaded;
- the requested number of steps ran;
- the checkpoint version matches;
- the report is complete and valid;
- **and**, when the run requires closure, that the resume-state manifest it
  restored is complete.

If these conditions are not met, the migration does not become `succeeded`.

## Evidence levels

- **L3**: model and optimizer load and the target runs real training steps.
- **L4**: L3 plus a proven resume-state closure — every required component
  (model, optimizer, global step, dataloader, RNG) is declared present in the
  manifest the target restored.

L4 is not implied by a successful L3 check. `stateComplete=false` must remain
visible and must not be presented as complete state restoration.

The L4 gate is opt-in per run (`requireStateComplete`). Gate off keeps the run
an L3 claim; gate on turns any unproven closure into a failure
(`StateClosureIncomplete`) instead of a passing run that silently downgrades.

## Report

Schema v2 binds the report to a resume-state manifest digest:

```json
{
  "schemaVersion": "2",
  "migrationUID": "default/job42",
  "version": "step-100",
  "rank": 0,
  "role": "worker",
  "modelLoaded": true,
  "optimizerLoaded": true,
  "stepsRan": 10,
  "stateComplete": true,
  "ok": true,
  "error": "",
  "resumeState": {
    "manifestDigest": "sha256:...",
    "closure": "COMPLETE",
    "components": [
      { "kind": "dataloader", "required": true, "present": true },
      { "kind": "global_step", "required": true, "present": true }
    ]
  }
}
```

`stateComplete` is derived from `resumeState.closure`; a report that disagrees
with itself is rejected. Component digests stay in the manifest — the report
carries the manifest digest once. See [the resume-state
contract](RESUME-STATE.md) for how a manifest is produced and classified.

After installing the Aevrel Python package, the target training script can
write a report:

```python
from aevrel.state import build_manifest
from aevrel.verification import ResumeStateEvidence, write_target_ready_report

manifest = build_manifest(
    checkpoint_uid=uid,
    version=version,
    metadata_digest=metadata_digest,
    world_size=world_size,
    present={"model": "dcp", "optimizer": "dcp"},
    digests={"model": model_digest, "optimizer": optimizer_digest},
)
write_target_ready_report(
    model_loaded=True,
    optimizer_loaded=True,
    steps_ran=10,
    resume_state=ResumeStateEvidence.from_manifest(manifest),
)
```

Or use the CLI with the manifest the target wrote:

```bash
aevrel target-report \
  --model-loaded \
  --optimizer-loaded \
  --steps-ran "$AEVREL_VALIDATION_STEPS" \
  --resume-state "$AEVREL_RESUME_STATE_PATH"
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
  --steps "$AEVREL_VALIDATION_STEPS" \
  --resume-state-out /tmp/resume-state.json
aevrel target-report \
  --model-loaded \
  --optimizer-loaded \
  --steps-ran "$AEVREL_VALIDATION_STEPS" \
  --resume-state /tmp/resume-state.json
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
- `StateClosureIncomplete`: closure was required but not proven, with
  `reasonCodes` (`RESUME_COMPONENT_MISSING`, `RESUME_STATE_UNVERIFIED`,
  `RESUME_MANIFEST_INVALID`);
- `JobFailed` or `Timeout`: the target Job failed or timed out;
- `InvalidReport`: malformed JSON, unknown fields, duplicate ranks, a
  self-contradicting report, or ranks that restored different resume-state
  manifests;
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
the checkpoint invalidates the old proof. `spec.requireStateComplete` turns the
drill into an L4 drill.

## Limitations

- The target must run in the controller's cluster in the current contract.
- Aevrel does not parse framework-specific optimizer formats.
- L4 state continuity is not inferred from an L3 result.
- A legacy schema v1 report is decoded only to be rejected with a precise
  message; a bare `stateComplete` boolean is a claim, not evidence.
- Target-ready is not a training performance benchmark.
