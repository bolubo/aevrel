# Development Map

Use this page to find the first file and the smallest relevant validation for a
change. For the architecture overview, see [Architecture](ARCHITECTURE.md).

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
| Documentation and onboarding | `README.md`, `docs/` | `scripts/check_repo.py` | `python scripts/check_repo.py` |

## Safe order for a first change

1. Run [First contribution](FIRST-CONTRIBUTION.md).
2. Pick a starter issue with a clear test.
3. Add or adjust the test first.
4. Change only the code inside the issue boundary.
5. Run the narrow command above, then the full validation suite.

## Avoid mixing concerns

Do not combine controller state-machine changes, CRD/schema changes, LRS
behavior, Helm changes, and CLI changes in one starter pull request.
