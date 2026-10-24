# Aevrel 开发地图

这个页面用于回答“我要改的地方在哪里”。它不是完整架构文档；架构见
[ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)。

## 入口地图

| 区域 | 第一入口 | 主要测试 | 最小验证 |
|---|---|---|---|
| Python CLI / demo | `src/aevrel/cli.py`、`src/aevrel/demo.py` | `tests/unit/test_cli.py`、`tests/unit/test_cli_subprocess.py` | `python -m pytest -q tests/unit/test_cli.py tests/unit/test_cli_subprocess.py` |
| DCP / local adapter | `src/aevrel/local/dcp.py`、`src/aevrel/dcp/` | `tests/unit/test_dcp_inventory.py`、`tests/integration/test_local_4to3.py` | `python -m pytest -q tests/unit/test_dcp_inventory.py tests/integration/test_local_4to3.py` |
| Readiness / verification | `src/aevrel/local/readiness.py`、`src/aevrel/verification/` | `tests/unit/test_control_schemas.py`、`tests/integration/test_readiness_cli.py` | `python -m pytest -q tests/unit/test_control_schemas.py tests/integration/test_readiness_cli.py` |
| Go control plane | `internal/control/`、`internal/controller/` | `internal/control/*_test.go`、`internal/controller/*_test.go` | `go test ./internal/control ./internal/controller` |
| Kueue adapter | `internal/kueueadapter/` | `internal/kueueadapter/*_test.go` | `go test ./internal/kueueadapter` |
| CRD / API | `api/v1/`、`config/crd/bases/` | `tests/unit/test_crd.py` | `python -m pytest -q tests/unit/test_crd.py` |
| Helm | `deploy/helm/aevrel/` | `tests/unit/test_helm_chart.py` | `helm lint deploy/helm/aevrel` |
| Cross-language schemas | `spec/v1/` | `tests/unit/test_contracts.py` | `python -m pytest -q tests/unit/test_contracts.py` |
| State closure / LRS | `src/aevrel/state/`、`src/aevrel/lrs/` | `tests/unit/test_lrs_*.py`、`tests/unit/test_state_closure.py` | `python -m pytest -q tests/unit/test_state_closure.py tests/unit/test_lrs_state_closure.py` |
| Docs / onboarding | `README.md`、`docs/` | `scripts/check_repo.py` | `python scripts/check_repo.py` |

## 第一次改动的安全顺序

1. 先运行 [FIRST-CONTRIBUTION](FIRST-CONTRIBUTION.zh-CN.md)；
2. 选择有明确测试的 starter issue；
3. 先写/调整测试，再改实现；
4. 只改 issue 指定边界；
5. 最后运行对应行的最小验证，再运行全量测试。

## 不要从这些地方开始

- 不要先改 `internal/controller` 的状态机，除非 issue 明确要求；
- 不要先改 CRD group、schema 或迁移语义；
- 不要把 LRS、Kueue、Helm 和 CLI 的行为混在一个 starter PR；
- 不要用真实 checkpoint 或生产凭据跑 starter issue。
