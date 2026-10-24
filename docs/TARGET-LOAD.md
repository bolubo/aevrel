# Target DCP Load

Target load is the final local check: can a target process read the migrated
DCP data and construct tensors?

## Rank-local smoke test

Run this on a target node:

```bash
aevrel-target-load \
  --checkpoint /targets/job42/rank0 \
  --metadata-dir /targets/job42/metadata \
  --rank 0 \
  --json
```

A successful result reports:

```json
{
  "status": "ok",
  "mode": "rank-local",
  "tensors": 3,
  "bytes": 123456,
  "duration_seconds": 0.42
}
```

This verifies that metadata and the rank-local `.distcp` chunks are readable
and that PyTorch DCP can deserialize the local data. It does not replace a
model/optimizer load inside the training process.

## Full smoke test

If every rank can access one complete DCP directory:

```bash
aevrel-target-load \
  --checkpoint /targets/job42/full \
  --json
```

Full mode allocates complete CPU tensors and can use significant memory. For
large models, use the rank-local smoke first.

## Load in training code

The target world size and DeviceMesh must match the migration target.

For a regular module:

```python
import torch.distributed.checkpoint as dcp

state_dict = model.state_dict()
dcp.load(state_dict, checkpoint_id=target_checkpoint)
model.load_state_dict(state_dict)
```

For FSDP/DTensor, use the PyTorch state-dict helpers rather than treating the
metadata as a plain state dict:

```python
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_state_dict,
    set_state_dict,
)

options = StateDictOptions(full_state_dict=False, cpu_offload=True)
model_state, optim_state = get_state_dict(model, optimizer, options=options)
dcp.load(
    {"model": model_state, "optimizer": optim_state},
    checkpoint_id=target_checkpoint,
)
set_state_dict(
    model,
    optimizer,
    model_state_dict=model_state,
    optim_state_dict=optim_state,
    options=options,
)
```

The repository's GPU scripts are examples; replace the model, optimizer, and
state keys with the real training code.

## Target roots

Rank-local migration commonly produces:

```text
/targets/job42/rank0/.metadata
/targets/job42/rank0/__0_0.distcp
/targets/job42/rank0/w/rank0.bin
```

Each target Pod must mount its corresponding rank root and point
`checkpoint_id` at that path. Do not assume all ranks share one directory
unless the target storage is actually shared.

## Common failures

- Missing `.metadata`: the migration may still be staging, or the target root
  is mounted incorrectly.
- Missing rank chunk: the rank did not finalize/commit; do not copy another
  rank's files by hand.
- Shape or dtype mismatch: the target model or topology differs from the source.
- OOM during full load: use rank-local smoke and the real distributed loader.
- Source changed unexpectedly: stop, preserve the source, and collect a
  support bundle.
