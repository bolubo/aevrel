#!/usr/bin/env python3
"""Line 2 acceptance: the scheduler/runtime contract v1 must be real.

Each check corresponds to one acceptance criterion of the Line 2 plan. The
script is deliberately static: it proves that the artefacts, the registrations
and the invariants are present, and it fails loudly when one is missing or has
drifted back into an unsafe shortcut.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONTRACT = ROOT / "docs" / "scheduler-runtime-contract-v1.md"
INDEX = ROOT / "docs" / "INDEX.md"
ROADMAP = ROOT / "docs" / "ROADMAP.md"
CATALOG = ROOT / "docs" / "FAILURE-CODES.md"
KUEUE_DOC = ROOT / "docs" / "KUEUE-PREEMPTION-GUARD.md"
CHECK_REPO = ROOT / "scripts" / "check_repo.py"

ADAPTER = ROOT / "internal" / "kueueadapter"
FACTS = ADAPTER / "facts.go"
CONFORMANCE = ADAPTER / "conformance.go"
PROJECTOR = ADAPTER / "projector.go"
TRANSLATORS = ADAPTER / "translators.go"
SOURCE = ADAPTER / "source.go"
POLICY = ADAPTER / "policy.go"
CONTRACT_GO = ADAPTER / "contract.go"
RECONCILER = ADAPTER / "reconciler.go"

CTL = ROOT / "cmd" / "ctl" / "kueue.go"
INTEGRATION = ROOT / "tests" / "integration" / "kueue_contract_test.go"

CONSUMER_FIELDS = (
    "  Complete:",
    "  Fresh:",
    "  Loadable:",
    "  Movable:",
    "  ObservedAt:",
    "  Source:",
    "  ReasonCodes:",
    "  RestoreMode:",
    "  EstimatedRTO:",
    "  Selected:",
    "  Schema:",
)

VIOLATION_CODES = (
    "FreshWithoutLoadabilityVerdict",
    "RestoreWithoutDrillProof",
    "RTOWithoutCompleteInventory",
    "RTOMustBePositiveOrNull",
    "SchemaVersionMismatch",
    "MissingSource",
    "UnknownFactSource",
    "MissingObservedAt",
    "FallbackClaimsVerifiedFacts",
    "FallbackClaimsRestoreMode",
    "UnknownSourceClaimsFacts",
    "AmbiguousRecordSelection",
)

# Every fact label lives inside the v1 namespace. The schema-version label is
# the one exception: it is what names the namespace in the first place.
ENVELOPE_NAMESPACE = "aevrel.dev/v1-"
SCHEMA_LABEL = "LabelEnvelopeSchemaVersion"

FAIL_CLOSED_MARKERS = (
    "no `CheckpointRecord`, no annotations",
    "annotations malformed",
    "several records, no latest",
    "observation older than",
    "fails conformance",
    "cannot read Kubernetes",
    "withdrawn",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def check_contract_document(errors: list[str]) -> None:
    if not CONTRACT.is_file():
        errors.append(f"missing {CONTRACT.relative_to(ROOT)}")
        return
    text = read(CONTRACT)
    for section in ("## A.", "## B.", "## C.", "## D.", "## E.", "## F.", "## G."):
        if section not in text:
            errors.append(f"contract is missing section {section}")
    if "aevrel.dev/v1" not in text:
        errors.append("contract does not name the schema version")
    if "advisory" not in text:
        errors.append("contract does not state that restoreMode is advisory")


def check_envelope_registry(errors: list[str]) -> None:
    if not CONTRACT_GO.is_file():
        errors.append("missing internal/kueueadapter/contract.go")
        return
    pattern = re.compile(r'^\s*(LabelEnvelope[A-Za-z0-9_]+)\s*=\s*"([^"]+)"', re.M)
    labels = pattern.findall(read(CONTRACT_GO))
    if not labels:
        errors.append("no envelope labels are declared")
    text = read(CONTRACT) if CONTRACT.is_file() else ""
    for name, key in labels:
        if not key.startswith("aevrel.dev/"):
            errors.append(f"{name} is outside the aevrel.dev namespace")
        if name != SCHEMA_LABEL and not key.startswith(ENVELOPE_NAMESPACE):
            errors.append(
                f"{name} ({key}) is outside the {ENVELOPE_NAMESPACE} namespace"
            )
        if key not in text:
            errors.append(f"{name} ({key}) is not documented in the contract")


def check_explain_surface(errors: list[str]) -> None:
    if not CTL.is_file():
        errors.append("missing cmd/ctl/kueue.go")
        return
    text = read(CTL)
    if "Checkpoint facts:" not in text:
        errors.append("kueue explain does not print a facts block")
    for marker in CONSUMER_FIELDS:
        if marker not in text:
            errors.append(f"kueue explain does not report {marker.strip()}")
    if '"schemaVersion"' not in text:
        errors.append("kueue explain --json does not publish a schemaVersion")
    if "CheckpointFacts" not in text:
        errors.append("kueue explain --json does not publish structured facts")


def check_policy_untouched(errors: list[str]) -> None:
    if not POLICY.is_file():
        errors.append("missing internal/kueueadapter/policy.go")
        return
    text = read(POLICY)
    for forbidden in ("Facts", "CheckpointFacts", "restoreMode", "EstimatedRTO"):
        if forbidden in text:
            errors.append(f"the binary policy references {forbidden}")


def check_conformance_gate(errors: list[str]) -> None:
    if not CONFORMANCE.is_file():
        errors.append("missing internal/kueueadapter/conformance.go")
        return
    text = read(CONFORMANCE)
    for code in VIOLATION_CODES:
        if code not in text:
            errors.append(f"conformance gate is missing violation {code}")
    if "func ValidateFacts" not in text:
        errors.append("conformance gate has no ValidateFacts entry point")
    if not PROJECTOR.is_file():
        errors.append("missing internal/kueueadapter/projector.go")
        return
    projector = read(PROJECTOR)
    if "facts.Conformance.Valid" not in projector:
        errors.append("the projector does not consult the conformance verdict")
    if "ProjectFacts" not in projector:
        errors.append("the projector cannot publish the envelope")


def check_fail_closed(tokens: list[str]) -> None:
    if CONTRACT.is_file():
        text = read(CONTRACT)
        for marker in FAIL_CLOSED_MARKERS:
            if marker not in text:
                tokens.append(f"fail-closed matrix is missing {marker!r}")
    else:
        tokens.append("fail-closed matrix has no contract to live in")
    if SOURCE.is_file():
        source = read(SOURCE)
        for marker in (
            "FactSourceCheckpointRecordAmbiguous",
            "FactSourceAnnotationFallback",
            "FactSourceUnknown",
        ):
            if marker not in source and marker not in read(FACTS):
                tokens.append(f"source resolution never reaches {marker}")
    else:
        tokens.append("missing internal/kueueadapter/source.go")


def check_restore_mode_provenance(errors: list[str]) -> None:
    if not FACTS.is_file():
        errors.append("missing internal/kueueadapter/facts.go")
        return
    text = read(FACTS)
    if "control.DecideRescue(" not in text:
        errors.append("restoreMode is not derived from control.DecideRescue")
    if text.count("control.DecideRescue(") < 3:
        errors.append("not every fact source derives its action from DecideRescue")


def check_evidence_binding(errors: list[str]) -> None:
    if not FACTS.is_file():
        errors.append("missing internal/kueueadapter/facts.go")
        return
    text = read(FACTS)
    if "readiness.Reasons" not in text:
        errors.append("reasonCodes are not copied from authoritative readiness")
    if "factReasonCodes" not in text:
        errors.append("reasonCodes have no single mapping function")
    contract = read(CONTRACT) if CONTRACT.is_file() else ""
    if "1:1" not in contract:
        errors.append("contract does not state the 1:1 reason-code mapping")


def check_consumer_honesty(errors: list[str]) -> None:
    if not TRANSLATORS.is_file():
        errors.append("missing internal/kueueadapter/translators.go")
        return
    text = read(TRANSLATORS)
    if text.count("Supported: false") < 2:
        errors.append("Volcano and Kubeflow must both report unsupported")
    for consumer in ("VolcanoTranslator", "KubeflowTranslator", "KueueTranslator"):
        if consumer not in text:
            errors.append(f"missing translator {consumer}")
    doc = read(KUEUE_DOC) if KUEUE_DOC.is_file() else ""
    contract = read(CONTRACT) if CONTRACT.is_file() else ""
    if "Reserved. Not implemented." not in contract:
        errors.append("contract does not mark the reserved consumers honestly")
    if "envelope-labels" not in doc:
        errors.append("Kueue documentation does not describe the opt-in flag")


def check_integration(errors: list[str]) -> None:
    if not INTEGRATION.is_file():
        errors.append("missing tests/integration/kueue_contract_test.go")
        return
    text = read(INTEGRATION)
    if "TestKueueContract" not in text:
        errors.append("the integration test does not assert the contract")
    for state in ("ready", "unverified", "stale", "no record"):
        if state not in text:
            errors.append(f"the integration test does not cover the {state} state")


def check_registrations(errors: list[str]) -> None:
    if not CHECK_REPO.is_file():
        errors.append("missing scripts/check_repo.py")
        return
    text = read(CHECK_REPO)
    if "scheduler-runtime-contract-v1.md" not in text:
        errors.append("repo-check does not register the contract document")
    if "_check_contract_registry" not in text:
        errors.append("repo-check has no envelope label registry guard")
    index = read(INDEX) if INDEX.is_file() else ""
    if "scheduler-runtime-contract-v1.md" not in index:
        errors.append("docs/INDEX.md does not link the contract")
    roadmap = read(ROADMAP) if ROADMAP.is_file() else ""
    if "scheduler-runtime-contract-v1.md" not in roadmap:
        errors.append("docs/ROADMAP.md does not link the contract")
    catalog = read(CATALOG) if CATALOG.is_file() else ""
    if "Scheduler envelope" not in catalog:
        errors.append("docs/FAILURE-CODES.md does not document the envelope codes")


def check_reconciler_wiring(errors: list[str]) -> None:
    if not RECONCILER.is_file():
        errors.append("missing internal/kueueadapter/reconciler.go")
        return
    text = read(RECONCILER)
    for marker in ("ProjectFacts", "ClearFacts", "FactProjector"):
        if marker not in text:
            errors.append(f"the reconciler never calls {marker}")


def main() -> int:
    checks = (
        ("contract document", check_contract_document),
        ("envelope registry", check_envelope_registry),
        ("explain surface", check_explain_surface),
        ("policy isolation", check_policy_untouched),
        ("conformance gate", check_conformance_gate),
        ("restore-mode provenance", check_restore_mode_provenance),
        ("evidence binding", check_evidence_binding),
        ("consumer honesty", check_consumer_honesty),
        ("integration evidence", check_integration),
        ("registrations", check_registrations),
        ("reconciler wiring", check_reconciler_wiring),
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

    # The fail-closed matrix is checked last because it spans two artefacts.
    fail_closed: list[str] = []
    check_fail_closed(fail_closed)
    if fail_closed:
        failures.extend(f"fail-closed matrix: {error}" for error in fail_closed)
        print("FAIL - fail-closed matrix")
    else:
        print("PASS - fail-closed matrix")

    for failure in failures:
        print(f"  {failure}")
    if failures:
        print(f"line2-acceptance FAILED ({len(failures)} issues)")
        return 1
    print("line2-acceptance ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
