from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def workflow() -> dict:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run_commands(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_ci_workflow_has_linux_python_go_and_helm_jobs() -> None:
    spec = workflow()
    assert set(spec["jobs"]) == {"python", "native", "go", "helm", "docker"}
    assert "push" in spec[True]
    assert "workflow_dispatch" in spec[True]


def test_ci_workflow_gates_the_native_crate() -> None:
    """Rust is a first-class language here, so CI has to gate it.

    Formatting, lints and the golden equivalence suite all run as their own
    job; `clippy` is denied warnings so a new warning cannot slip through.
    """
    commands = run_commands(workflow()["jobs"]["native"])
    assert "cargo fmt --check" in commands
    assert "cargo clippy --release --all-targets -- -D warnings" in commands
    assert "cargo test --release" in commands


def test_ci_workflow_builds_the_native_artefacts_before_pytest() -> None:
    """The data plane has no Python fallback, so the build must come first."""
    commands = run_commands(workflow()["jobs"]["python"])
    build = "cargo build --release --manifest-path native/aevrel-fast/Cargo.toml"
    assert build in commands
    # Match the step by what it runs, not by its exact flags: the suite gained
    # `-n auto --dist loadfile` and would gain more, while the ordering rule
    # this test exists to protect is unchanged.
    pytest_steps = [
        index for index, command in enumerate(commands) if "pytest" in command
    ]
    assert pytest_steps, "the python job must run pytest"
    assert commands.index(build) < pytest_steps[0]


def test_python_job_does_not_shell_out_to_go_or_helm() -> None:
    """The Python job must stay Python-only.

    Every Go or Helm call here cold-compiled the Kubernetes tree because the
    job has no toolchain cache. Those checks now run in the jobs that already
    own the tool: `go test ./...`, `helm lint` and `go run ./cmd/ctl`.
    """
    commands = run_commands(workflow()["jobs"]["python"])
    for command in commands:
        assert "go test" not in command
        assert "go run" not in command
        assert "helm lint" not in command


def test_ci_workflow_lints_the_non_tls_chart_in_the_helm_job() -> None:
    """The non-TLS template branch must keep a non-skipping lint.

    The Python suite lints it too, but only when `helm` happens to be on the
    runner; the helm job installs the tool, so this step is the real gate.
    """
    commands = run_commands(workflow()["jobs"]["helm"])
    assert "helm lint deploy/helm/aevrel --set tls.enabled=false" in commands


def test_ci_workflow_dry_runs_the_ctl_in_the_go_job() -> None:
    """`aevrelctl migrate --dry-run` is an end-to-end binary check.

    It moved out of the Python job, so the go job has to keep asserting that
    the rendered resource still carries `agentSelector`.
    """
    script = "\n".join(run_commands(workflow()["jobs"]["go"]))
    assert "go run ./cmd/ctl migrate -f examples/k8s/migration.yaml --dry-run" in script
    assert "grep -q agentSelector" in script

