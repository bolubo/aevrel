# GPU validation (2026-10-22)

## Environment

- Instance: Alibaba Cloud `ecs.gn8is-4x.16xlarge`, 4 x NVIDIA L20 48 GB
- Driver: `580.126.09`
- Python: `3.11.13`
- PyTorch: `2.11.0+cu128`
- CUDA runtime: `12.8`; NCCL: available; bf16: available
- Instance ID: `i-2ze3xrryab2etcc8uo9j`

## Results

All target loads printed `output_ok=True optimizer_state_loaded=True`.

| Case | Topology | dtype | Plan | Result |
|---|---|---|---|---|
| A | 4 -> 2 | fp32 | 24 chunks / 480 B | pass |
| B | 4 -> 3 | fp32 | 30 chunks / 480 B | pass |
| C | 4 -> 2 | bf16 | 24 chunks / 240 B | pass |
| D | 2 -> 4 | fp32 | 24 chunks / 480 B | pass |
| E | 4 -> 1 | fp32 | 24 chunks / 480 B | pass |
| F | 1 -> 4 | fp32 | 24 chunks / 480 B | pass |
| G | 4 -> 3 | fp32, 8192-wide bulk layer | 66 chunks / 768.1 MiB | pass |

For every case, the aggregate source SHA256 was identical before and after
migration:

```text
A 04ade142bcfd7b88cc04cbf41d782b3664f633255323f83b162c210a6c66112d
B 3a30c25af30232b6d0594b304a9806cb82a5a2d6aabcfb807c19130f4cff7a8f
C 0fbcbc66f9075a9ab41a1203513727a0946fe91e10cf26f2856b39fcb93e0436
D 5c4813212641dbfab9c9e52c74b3760c5b95a4a9966c4d8c2d6e4f6a394fc787
E (see case directory)
F (see case directory)
G 67ea32f8d343bcb13f590d4260279eed86f290732c2e491614c79d5c8b6bc4ba
```

Migration verification was `bitwise passed` in every case, although bf16
target output was checked with the documented `atol=1e-2` tolerance.

Case G used `--bulk-dim 8192`, a 769 MiB source/target checkpoint. Source save
took 11.67 s, migration took 6.99 s for the transfer plus 10.43 s wall time,
and target load took 6.86 s. This is the first non-toy size evidence and still
does not represent a production model or network.

Cold-cache comparison on the same instance:

- DCP native load-time reshard: `21.35 s` median (`20.97`, `21.73`);
- `cp -a` plus DCP load: `5.15 s + 22.62 s = 27.77 s`;
- ShardFerry migrate plus DCP load: `10.43 s + 21.84 s = 32.27 s`.

ShardFerry is slower on a single host with a shared filesystem. The current
product claim must therefore be cross-domain resumability/verifiability, not
same-host speed. See `docs/BENCHMARK.zh-CN.md` for the full matrix.

Raw logs and fixtures are under
`/root/lines/oss/aicluster/evidence/gpu-2026-10-22/`.

## Compatibility fixes found by the GPU run

PyTorch 2.11 packs multiple logical tensors into one `.distcp` file using
`_StorageInfo.offset` / `length`. The range reader previously inspected only
the first ZIP segment, so different tensors in the same rank file could read
the same bytes. `inspect_zip_tensor` now accepts an explicit storage segment.

The same run also exposed three format details:

- ZIP tensor payloads may be padded beyond the logical tensor byte count.
- FSDP optimizer metadata contains `BytesStorageMetadata` scalar entries.
- Optimizer `step` is a zero-dimensional tensor and cannot use `Shard(0)`.

These entries are now preserved as opaque/scalar checkpoint entries while
non-scalar tensor chunks are resharded normally.

## Test evidence

- Full Python suite passed `273 passed, 5 skipped` after the DCP fixes.
- Distributed DCP and DCP HTTP integration tests: `2 passed`.
- After the optional `--bulk-dim` script extension, affected DCP/script/CLI
  tests passed `20 passed`.
- A final full-suite rerun hit an existing timing assertion in
  `test_cutover_service_auto_follows_in_background`
  (`follow_synced_versions == 1`, expected `2`); it is reproducible in
  isolation and does not touch the DCP code changed here.
- CPU-only skips remained for Helm and the optional native release binary.

## Still not proven

- 128-rank or multi-node GPU topology.
- Cross-host GPU checkpoint transfer.
- Training-loop resume beyond the model/optimizer/forward smoke test.
- Production-scale model sizes, network throughput, and failure injection.
