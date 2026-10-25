# Repository tooling

`scripts/` holds the automation the repository itself relies on: CI steps,
smoke tests, and documented walkthroughs. Everything experimental — benchmarks,
probes, GPU and multi-node runs, LRS demos — lives in
[`experiments/`](../experiments/README.md) instead and may be deleted without
notice.

| Script | Used by | Purpose |
|---|---|---|
| `check_repo.py` | CI, PR template, docs | repository and public-docs hygiene (required files, links, English-only Markdown) |
| `check_release.py` | CI, PR template, docs | release metadata consistency (versions, images, README commands) |
| `acceptance.py` | CI | core acceptance path |
| `product_acceptance.py` | CI, README, CONTRIBUTING | end-to-end product acceptance |
| `first_run_acceptance.py` | CI, first-contribution guide | the first-run path a new contributor executes |
| `live_acceptance.py` | `tests/unit/test_live_acceptance.py` | live rank-split acceptance evidence |
| `lrs_cpu4_live.py` | `tests/integration/test_lrs_cpu4_live.py` | CPU live rank-split proof (4 -> 3 -> 4) with evidence output |
| `rescue_demo.py` | `docs/RESCUE-DEMO.md` | rescue decision walkthrough |
| `ab_target.py`, `benchmark.py`, `planner_benchmark.py`, `summarize_reports.py` | `tests/unit/test_scripts.py` | benchmark and report helpers kept under a `--help` smoke test |
| `gpu_source_save.py`, `gpu_target_load.py`, `gpu_codec_probe.py` | `tests/unit/test_scripts.py` | GPU experiments that still carry a smoke test |
| `kind-kubectl.sh` | local Kubernetes testing | kind cluster helper |

A script belongs here when repository automation or a documented workflow
references it. New benchmarks, probes, and demos go to `experiments/`.
