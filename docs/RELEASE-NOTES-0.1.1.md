# shardferry 0.1.1 — GPU-validated Alpha

This release turns the 0.1 preview into a reproducible GPU-validated alpha.
It is still an alpha, but the core data path and the Kubernetes control plane
now have concrete evidence behind them.

## Highlights

- Real FSDP/DCP target loads on 4×NVIDIA L20:
  `4->2`, `4->3`, `4->1`, `1->4` fp32 and `4->2` bf16.
- 769 MiB `4->3` checkpoint case: source SHA256 unchanged, bitwise target,
  target model/optimizer load.
- PyTorch 2.11 compatibility: segmented `.distcp` files, padded payloads,
  `BytesStorageMetadata`, and zero-dimensional optimizer `step`.
- Two-kind cross-cluster control plane, per-cluster gateway tokens, mTLS,
  selector discovery, rank-level status, plan preview and safe lifecycle
  commands.
- Controller restart recovery, target failure handling, commit repair,
  migration proof and support bundle.
- Python wheel, controller/gateway/agent images, OCI Helm chart and release
  checks.
- Release assets include `shardferry-0.1.1-evidence.tar.gz`, a compact bundle
  of GPU case logs, SHA256 digests and test logs (no checkpoint payloads).
  The packaged Helm chart is attached as `shardferry-0.1.1.tgz`.

## Evidence

- Python: `273 passed / 5 skipped` in the GPU validation environment;
  final release venv: `257 passed / 29 skipped` (torch-dependent tests skipped
  without torch).
- Go: `go test ./...`, `go vet ./...`, controller/ctl/gateway builds pass.
- GPU cases: all target ranks report
  `output_ok=True optimizer_state_loaded=True`.
- Cross-cluster: 16→8, synthetic 128→64 and 1 GiB control/data-plane runs.

See [`BENCHMARK.zh-CN.md`](BENCHMARK.zh-CN.md).

## Honest limitations

- GPU validation is one 4-GPU host; physical 128-rank GPU clusters and
  cross-host GPU transfer remain future work.
- 128→64 is a control-plane/data-plane scale proof, not a physical 128-GPU
  benchmark.
- On one host with a shared filesystem, native DCP load-time resharding is
  faster. shardferry optimizes cross-domain movement, resume and proof.
- Target readiness is model/optimizer/forward smoke, not full dataloader, RNG
  and scheduler continuation.
- Online/live cutover is not a 0.1.1 production promise.

## Upgrade from 0.1.0

`v0.1.0` remains available but should be treated as superseded. Use the
`0.1.1` wheel, images and Helm chart. No manual checkpoint rewrite is required.

## Release asset SHA256

```text
e3da00dba8fe7170154fc9c890d37768d48b42c24a6ac14e3ef64667944ec586  shardferry-0.1.1-py3-none-any.whl
232003deabe287cf208d978765869bdb44c4b1e7392362bb34b8d83f314a2be0  shardferry-0.1.1.tgz
03f88013a4776665a5bb1cf19a9b61b98875274e3d280d68fc45049647ff3e8c  shardferry-0.1.1-evidence.tar.gz
```
