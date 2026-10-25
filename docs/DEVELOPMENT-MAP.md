# Development Map

Use this page to find the first file and the smallest relevant validation for a
change. For the architecture overview, see [Architecture](ARCHITECTURE.md).

`make dev` sets up the toolchain and `make check` runs the same gates CI runs.
Every command below assumes that environment.

| Area | First entry points | Main tests | Validation |
|---|---|---|---|
| Python CLI and demo | `src/aevrel/cli.py`, `src/aevrel/demo.py` | `tests/unit/test_cli.py`, `tests/unit/test_cli_subprocess.py` | `python -m pytest -q tests/unit/test_cli.py tests/unit/test_cli_subprocess.py` |
| DCP and local adapters | `src/aevrel/local/dcp.py`, `src/aevrel/dcp/` | `tests/unit/test_dcp_inventory.py`, `tests/integration/test_local_4to3.py` | `python -m pytest -q tests/unit/test_dcp_inventory.py tests/integration/test_local_4to3.py` |
| Readiness and verification | `src/aevrel/local/readiness.py`, `src/aevrel/verification/` | `tests/unit/test_control_schemas.py`, `tests/integration/test_readiness_cli.py` | `python -m pytest -q tests/unit/test_control_schemas.py tests/integration/test_readiness_cli.py` |
| Go control plane | `internal/control/`, `internal/controller/` | `internal/control/*_test.go`, `internal/controller/*_test.go` | `go test ./internal/control ./internal/controller` |
| Kueue adapter | `internal/kueueadapter/` | `internal/kueueadapter/*_test.go` | `go test ./internal/kueueadapter` |
| CRDs and API | `api/v1/`, `config/crd/bases/` | `tests/unit/test_crd.py` | `python -m pytest -q tests/unit/test_crd.py` |
| Helm | `deploy/helm/aevrel/` | `tests/unit/test_helm_chart.py` | `helm lint deploy/helm/aevrel` |
| Cross-language schemas | `spec/v1/`, `spec/*.schema.json` | `tests/unit/test_contracts.py` | `python -m pytest -q tests/unit/test_contracts.py` |
| State closure and LRS | `src/aevrel/state/`, `src/aevrel/lrs/` | `tests/unit/test_state_closure.py`, `tests/unit/test_lrs_*.py` | `python -m pytest -q tests/unit/test_state_closure.py tests/unit/test_lrs_state_closure.py` |
| Native hot path | `native/aevrel-fast/` | `native/aevrel-fast/src/*_test.rs`, `tests/unit/test_native.py` | `cargo test --manifest-path native/aevrel-fast/Cargo.toml` |
| Documentation and onboarding | `README.md`, `docs/` | `scripts/check_repo.py` | `python scripts/check_repo.py` |
| Repository tooling and experiments | `scripts/`, `experiments/` | `scripts/check_repo.py`, the `experiments/` guard test | `make check` |

## Language boundaries

Every area has one authoritative language. Choose the language by the job,
not by what a file used to be.

| Role | Language | Home | Owns |
|---|---|---|---|
| Control-plane authority | Go | `cmd/`, `internal/` | controllers, `aevrelctl`, gateway, Kueue adapter, scheduling decisions |
| Data-plane adapters and glue | Python | `src/aevrel/` | CLI and TUI, DCP/storage adapters, evidence collection, schema validation, tests |
| Native hot path | Rust | `native/aevrel-fast/` | content-defined chunking, SHA-256/CAS, byte-run coalescing, source checksum verification, proof Merkle/HMAC, LRS kernels |
| External-library glue | C/C++ (FFI only) | `native/ffi/` | thin shims for NCCL, PyTorch-CUDA, GPU codecs; never project-owned hot-path code |

Rules:

- C/C++ exists only to bind external libraries. New `.c`, `.cc`, `.cxx`,
  `.cpp`, `.h`, or `.hpp` files outside `native/ffi/` fail CI.
- Go never owns byte, hash, or numeric kernels. Python delegates hot kernels
  to Rust instead of reimplementing them.
- A missing Rust binary is a hard failure, never a silent fall back to Python.
  Every migrated kernel ships with a golden-equivalence test.
- `scripts/` holds repository tooling only; shipped logic belongs in
  `src/aevrel/` or `native/`. Each script is registered with the consumer that
  references it in the `scripts/` registry README.
- `experiments/` is disposable: benchmarks, probes, GPU and multi-node runs,
  and demos that no workflow, test, or script references. Promote a file to
  `scripts/`, or fold its logic into `src/` or `native/` with tests, before
  anything depends on it; a test fails if something under `experiments/` is
  referenced from CI, tests, or `scripts/`.
- Legacy exceptions exist (for example a duplicated chunking implementation)
  and are being removed. Do not add new exceptions.

## Safe order for a first change

1. Run [First contribution](FIRST-CONTRIBUTION.md).
2. Pick a starter issue with a clear test.
3. Add or adjust the test first.
4. Change only the code inside the issue boundary.
5. Run the narrow command above, then the full validation suite.
