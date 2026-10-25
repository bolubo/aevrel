#!/usr/bin/env python3
"""Line 3 acceptance: a published readiness fact must be evidence-bound.

Each check corresponds to one acceptance criterion of the Line 3 plan. The
script is static on purpose: it proves that the gates, the anchors, the shared
fixtures and the registrations exist in both implementations, and it fails when
one side drifts back into trusting a claim instead of its evidence.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SEMANTICS = ROOT / "docs" / "READINESS-SEMANTICS.md"
CATALOG = ROOT / "docs" / "FAILURE-CODES.md"
CONTRACT = ROOT / "docs" / "scheduler-runtime-contract-v1.md"
TARGET_READY = ROOT / "docs" / "TARGET-READY.md"
INDEX = ROOT / "docs" / "INDEX.md"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
CHANGELOG = ROOT / "CHANGELOG.md"
CHECK_REPO = ROOT / "scripts" / "check_repo.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"

GO_MODELS = ROOT / "internal" / "control" / "models.go"
GO_READINESS = ROOT / "internal" / "control" / "readiness.go"
GO_CONTROLLER = ROOT / "internal" / "controller" / "readiness.go"
GO_SCAN = ROOT / "internal" / "controller" / "agent_scan.go"
GO_CONFORMANCE = ROOT / "internal" / "kueueadapter" / "conformance.go"
GO_API = ROOT / "api" / "v1" / "checkpointrecord_types.go"

PY_MODELS = ROOT / "src" / "aevrel" / "local" / "models.py"
PY_READINESS = ROOT / "src" / "aevrel" / "local" / "readiness.py"
PY_AGENT = ROOT / "src" / "aevrel" / "agent" / "server.py"
PY_CLI = ROOT / "src" / "aevrel" / "commands" / "readiness.py"
PY_REPORT = ROOT / "src" / "aevrel" / "verification" / "target_report.py"

FIXTURES = ROOT / "tests" / "fixtures" / "readiness" / "conformance"
PY_CONFORMANCE_TEST = ROOT / "tests" / "unit" / "test_readiness_conformance.py"
GO_CONFORMANCE_TEST = ROOT / "internal" / "control" / "readiness_conformance_test.go"

EVIDENCE_REASONS = (
    "DRILL_STEPS_MISSING",
    "DRILL_RANK_COVERAGE_INCOMPLETE",
    "DRILL_RANK_COVERAGE_UNREPORTED",
    "DRILL_PROOF_STALE",
    "RESUME_MANIFEST_INVALID",
    "TARGET_TOPOLOGY_UNVERIFIED",
)

# Fixtures that pin one way a claim can lose its support. Each one is read by
# both implementations, so a semantic drift fails on one side or the other.
EVIDENCE_FIXTURES = (
    "drill-steps-missing",
    "drill-rank-coverage-incomplete",
    "drill-rank-coverage-unreported",
    "proof-stale",
    "manifest-identity-mismatch",
    "target-topology-unverified",
)

# Every readiness field has to name the component that produces its evidence,
# the evidence itself, and the rule that withdraws it. Field names here are the
# published JSON names, so a rename in the model surfaces as a doc failure.
EVIDENCE_FIELDS = ("complete", "fresh", "loadable", "movable", "state_complete")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def check_min_drill_steps(errors: list[str]) -> None:
    """A restored-bytes-and-stopped drill must not count as loadable."""
    go = read(GO_MODELS)
    if "MinDrillSteps = 1" not in go:
        errors.append("internal/control/models.go does not fix MinDrillSteps")
    if "drill.StepsRan < MinDrillSteps" not in read(GO_READINESS):
        errors.append("the Go assessment does not gate on the drill step count")
    py = read(PY_READINESS)
    if "MIN_DRILL_STEPS = 1" not in py:
        errors.append("src/aevrel/local/readiness.py does not fix MIN_DRILL_STEPS")
    if "drill.steps_ran < MIN_DRILL_STEPS" not in py:
        errors.append("the Python assessment does not gate on the drill step count")
    if "stepsRan" not in read(PY_REPORT) or "steps_ran" not in read(PY_REPORT):
        errors.append("the target-ready report does not carry step evidence")


def check_rank_coverage(errors: list[str]) -> None:
    """Every expected rank must be covered; one rank is not a world.

    Three answers matter and the gate must keep them apart: the drill named
    fewer ranks than the checkpoint has (a partial restore), the drill named no
    rank at all (no evidence), and the drill named every rank (no gap).
    """
    go_models = read(GO_MODELS)
    if "RanksReported []int" not in go_models:
        errors.append("DrillOutcome carries no rank identities")
    if "RanksExpected int" not in go_models:
        errors.append("DrillOutcome carries no expected world size")
    go = read(GO_READINESS)
    if "if len(drill.RanksReported) == 0 {" not in go:
        errors.append("the Go assessment does not treat a silent drill as unproven")
    if "ReasonDrillRankCoverageUnreported" not in go:
        errors.append("the Go assessment never reports unreported coverage")
    if "missing ranks %v" not in go:
        errors.append("the Go assessment does not name the missing ranks")
    py_models = read(PY_MODELS)
    if "ranks_reported: tuple[int, ...]" not in py_models:
        errors.append("the Python drill outcome carries no rank identities")
    if "ranks_expected: int" not in py_models:
        errors.append("the Python drill outcome carries no expected world size")
    py = read(PY_READINESS)
    if "if not drill.ranks_reported:" not in py:
        errors.append("the Python assessment does not treat a silent drill as unproven")
    if "DRILL_RANK_COVERAGE_UNREPORTED" not in py:
        errors.append("the Python assessment never reports unreported coverage")
    if "missing ranks" not in py:
        errors.append("the Python assessment does not name the missing ranks")
    controller = read(ROOT / "src" / "aevrel" / "commands" / "readiness.py")
    if "missing drill reports for ranks" not in controller:
        errors.append("the drill report aggregator accepts a partial world")
    if "ranks_reported = sorted(" not in controller:
        errors.append("the drill report aggregator drops the rank identities")


def check_proof_invalidation(errors: list[str]) -> None:
    """A proof must stop supporting a checkpoint once the checkpoint moves."""
    go = read(GO_READINESS)
    if "ReasonDrillProofStale" not in go:
        errors.append("the Go assessment never reports DRILL_PROOF_STALE")
    if "CheckpointMetadataDigest" not in go or "CheckpointVersion" not in go:
        errors.append("the Go proof check compares no checkpoint identity")
    py = read(PY_READINESS)
    if "DRILL_PROOF_STALE" not in py:
        errors.append("the Python assessment never reports DRILL_PROOF_STALE")
    api = read(GO_API)
    for field in (
        "LastDrillSteps",
        "LastDrillRanksReported",
        "LastDrillRanksExpected",
    ):
        if field not in api:
            errors.append(f"the persisted readiness status lacks {field}")
    # The proof identity has to survive on the record, or the staleness check
    # only fires during the reconcile that happened to read the drill.
    if "CheckpointVersion:        readiness.LastDrillVersion" not in read(
        GO_CONTROLLER
    ):
        errors.append("the controller does not carry the drill proof identity")


def check_identity_anchor(errors: list[str]) -> None:
    """The authoritative side must be able to reject a foreign manifest."""
    if "manifestIdentityGap" not in read(GO_READINESS):
        errors.append("the Go assessment has no manifest identity anchor")
    if "_manifest_identity_mismatch" not in read(PY_READINESS):
        errors.append("the Python assessment has no manifest identity anchor")
    scan = read(GO_SCAN)
    for marker in ("agentResumeCheckpoint", "resume.Checkpoint.MetadataDigest"):
        if marker not in scan:
            errors.append(f"the agent protocol never reads {marker}")
    if "manifest.checkpoint.uid" not in read(PY_AGENT):
        errors.append("the agent never emits the manifest identity")
    api = read(GO_API)
    if "ResumeCheckpointMetadataDigest" not in api:
        errors.append("the record does not persist the manifest identity")


def check_unknown_stays_unknown(errors: list[str]) -> None:
    """No drill means UNVERIFIED, never a silent pass."""
    go = read(GO_READINESS)
    if "ReadinessUnverified" not in go or "ReasonDrillMissing" not in go:
        errors.append("the Go assessment does not keep a missing drill unverified")
    py = read(PY_READINESS)
    if "ReadinessStatus.UNVERIFIED" not in py or "DRILL_MISSING" not in py:
        errors.append("the Python assessment does not keep a missing drill unverified")
    if "Reason" not in read(GO_READINESS) or "tri-state" not in read(SEMANTICS):
        errors.append("the tri-state rule for state_complete is not documented")


def check_fail_closed_status(errors: list[str]) -> None:
    """An evidence gap must block the target, not pass quietly."""
    go = read(GO_READINESS)
    if "evidenceBlocked || anchorBlocked || manifestBlocked" not in go:
        errors.append("the Go status ladder ignores the evidence gates")
    if "evidence_blocked or anchor_blocked or manifest_blocked" not in read(
        PY_READINESS
    ):
        errors.append("the Python status ladder ignores the evidence gates")


def check_movable_is_coarse(errors: list[str]) -> None:
    """movable is a coarse filter, and every surface has to say so.

    Three surfaces publish it — the semantics page, the v1 contract and the
    `kueue explain` output — and a consumer reads whichever one it finds
    first. One surface describing a migration guarantee is enough to undo the
    other two.
    """
    if "ReasonTargetTopologyUnverified" not in read(GO_READINESS):
        errors.append("the Go assessment attaches no topology caveat")
    if "TARGET_TOPOLOGY_UNVERIFIED" not in read(PY_READINESS):
        errors.append("the Python assessment attaches no topology caveat")
    if "coarse" not in read(PY_CLI):
        errors.append("the CLI does not label movable as a coarse check")
    semantics = read(SEMANTICS)
    if "coarse filter" not in semantics:
        errors.append("the semantics page does not label movable as a coarse filter")
    contract = read(CONTRACT)
    if "coarse filter, not a topology verdict" not in contract:
        errors.append("the v1 contract does not qualify movable as a coarse filter")
    go_cli = read(ROOT / "cmd" / "ctl" / "kueue.go")
    if "coarse filter, not a topology verdict" not in go_cli:
        errors.append("kueue explain does not qualify movable as a coarse filter")


def check_conformance_gate(errors: list[str]) -> None:
    """A restore claim needs a bound proof, not a drill name."""
    text = read(GO_CONFORMANCE)
    if "facts.LastDrillDigest" not in text:
        errors.append("the conformance gate accepts an unbound drill reference")
    if "bound drill proof" not in read(CONTRACT):
        errors.append("the contract does not require a bound drill proof")


def check_shared_fixtures(errors: list[str]) -> None:
    """Both implementations must read the same evidence fixtures."""
    names = {path.stem for path in FIXTURES.glob("*.json")}
    for name in EVIDENCE_FIXTURES:
        if name not in names:
            errors.append(f"missing evidence fixture {name}")
    for relative in (PY_CONFORMANCE_TEST, GO_CONFORMANCE_TEST):
        if not relative.is_file():
            errors.append(f"missing {relative.relative_to(ROOT)}")
    go_test = read(GO_CONFORMANCE_TEST)
    if "tests\", \"fixtures\", \"readiness\", \"conformance" not in go_test:
        errors.append("the Go conformance test does not read the shared fixtures")
    py_test = read(PY_CONFORMANCE_TEST)
    if "assert python_codes - go_codes == set()" not in py_test:
        errors.append("no machine check that the reason-code sets agree")


def check_evidence_provenance(errors: list[str]) -> None:
    """Each readiness field must name its producer, not only its evidence.

    A field whose producer is unnamed is a field nobody owns; that is exactly
    how an unset value quietly becomes a safe one.
    """
    semantics = read(SEMANTICS)
    rows = [
        line
        for line in semantics.splitlines()
        if line.startswith("| `") and line.rstrip().endswith("|")
    ]
    by_field: dict[str, list[str]] = {}
    for line in rows:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or not cells[0].startswith("`"):
            continue
        by_field[cells[0].strip("`")] = cells
    for field in EVIDENCE_FIELDS:
        cells = by_field.get(field)
        if cells is None:
            errors.append(f"the semantics table does not document {field}")
            continue
        if len(cells) != 4:
            errors.append(
                f"{field} has {len(cells)} columns; producer, evidence and "
                "invalidation are all required"
            )
            continue
        producer, evidence, invalidates = cells[1], cells[2], cells[3]
        if not producer or producer.lower().startswith("unknown"):
            errors.append(f"{field} does not name the component that produces it")
        if not evidence:
            errors.append(f"{field} does not state the evidence it requires")
        if not invalidates:
            errors.append(f"{field} does not state what invalidates it")


def check_documentation(errors: list[str]) -> None:
    semantics = read(SEMANTICS)
    if not semantics:
        errors.append("missing docs/READINESS-SEMANTICS.md")
        return
    if "## Evidence source and invalidation" not in semantics:
        errors.append("the semantics page has no evidence/invalidation table")
    catalog = read(CATALOG)
    for code in EVIDENCE_REASONS:
        if code not in catalog:
            errors.append(f"docs/FAILURE-CODES.md does not document {code}")
        if code not in semantics:
            errors.append(f"docs/READINESS-SEMANTICS.md does not mention {code}")
    if "MIN_DRILL_STEPS" not in semantics:
        errors.append("the semantics page does not state the minimum step count")
    if "checkpoint" not in read(TARGET_READY):
        errors.append("docs/TARGET-READY.md does not describe report identity")


def check_registrations(errors: list[str]) -> None:
    if "verify_line3_acceptance.py" not in read(CI):
        errors.append("CI does not run the line3 acceptance script")
    if "docs/READINESS-SEMANTICS.md" not in read(CHECK_REPO):
        errors.append("repo-check does not require the semantics page")
    if "READINESS-SEMANTICS.md" not in read(INDEX):
        errors.append("docs/INDEX.md does not link the semantics page")
    roadmap = read(ROADMAP)
    if "READINESS-SEMANTICS.md" not in roadmap:
        errors.append("docs/ROADMAP.md does not link the semantics page")
    changelog = read(CHANGELOG)
    for code in ("DRILL_STEPS_MISSING",):
        if code not in changelog:
            errors.append(f"CHANGELOG.md does not record {code}")


def check_reason_codes_are_declared(errors: list[str]) -> None:
    go_codes = set(
        re.findall(
            r'Reason[A-Za-z]+\s*=\s*"([A-Z][A-Z0-9_]*)"',
            read(GO_MODELS),
        )
    )
    py_codes = set(re.findall(r'^\s{4}([A-Z][A-Z0-9_]*) = "', read(PY_MODELS),
                   re.MULTILINE))
    for code in EVIDENCE_REASONS:
        if code not in go_codes:
            errors.append(f"Go does not declare reason {code}")
        if code not in py_codes:
            errors.append(f"Python does not declare reason {code}")


def main() -> int:
    checks = (
        ("minimum drill steps", check_min_drill_steps),
        ("rank coverage", check_rank_coverage),
        ("proof invalidation", check_proof_invalidation),
        ("manifest identity anchor", check_identity_anchor),
        ("unknown stays unknown", check_unknown_stays_unknown),
        ("fail-closed status", check_fail_closed_status),
        ("movable is coarse", check_movable_is_coarse),
        ("conformance gate", check_conformance_gate),
        ("shared fixtures", check_shared_fixtures),
        ("evidence provenance", check_evidence_provenance),
        ("documentation", check_documentation),
        ("reason code declarations", check_reason_codes_are_declared),
        ("registrations", check_registrations),
    )
    failures: list[str] = []
    for name, check in checks:
        errors: list[str] = []
        check(errors)
        if errors:
            failures.extend(f"{name}: {error}" for error in errors)
            print(f"FAIL - {name}")
        else:
            print(f"PASS - {name}")

    for failure in failures:
        print(f"  {failure}")
    if failures:
        print(f"line3-acceptance FAILED ({len(failures)} issues)")
        return 1
    print("line3-acceptance ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
