package control

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// These cases mirror tests/unit/test_readiness_evidence.py: each one is a
// claim that would otherwise read as true.

const (
	evidenceDigest      = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
	evidenceOtherDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)

func evidenceNow() time.Time {
	return time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)
}

func evidenceRecord(ranks int) CheckpointRecord {
	inventory := make([]RankInventory, 0, ranks)
	for index := 0; index < ranks; index++ {
		inventory = append(inventory, RankInventory{Rank: index, Bytes: 1024})
	}
	return CheckpointRecord{
		Checkpoint:     "/ckpt/step-100",
		Namespace:      "default",
		JobUID:         "job42",
		Version:        "step-100",
		Provider:       "dcp",
		FormatVersion:  "1",
		MetadataDigest: evidenceDigest,
		Ranks:          inventory,
		ObservedAt:     "2026-10-25T12:00:00Z",
		LatestMtime:    "2026-10-25T12:00:00Z",
		Complete:       true,
	}
}

func evidenceDrill() DrillOutcome {
	return DrillOutcome{
		Phase:                    "Succeeded",
		Succeeded:                true,
		StepsRan:                 10,
		ProofRef:                 "proof://drill/1",
		RanksReported:            []int{0, 1},
		RanksExpected:            2,
		CheckpointVersion:        "step-100",
		CheckpointMetadataDigest: evidenceDigest,
	}
}

func evidenceCodes(result CheckpointReadiness) []string {
	codes := make([]string, 0, len(result.Reasons))
	for _, reason := range result.Reasons {
		codes = append(codes, reason.Code)
	}
	return codes
}

func hasCode(codes []string, want string) bool {
	for _, code := range codes {
		if code == want {
			return true
		}
	}
	return false
}

func TestFullDrillEvidenceIsLoadable(t *testing.T) {
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  ptrDrill(evidenceDrill()),
	})
	if result.Status != ReadinessReady {
		t.Fatalf("status = %q (%v)", result.Status, evidenceCodes(result))
	}
	if result.Loadable == nil || !*result.Loadable {
		t.Fatalf("loadable = %v", result.Loadable)
	}
	if len(result.Reasons) != 0 {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
}

func TestSuccessWithoutStepsIsWithdrawn(t *testing.T) {
	drill := evidenceDrill()
	drill.StepsRan = 0
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q", result.Status)
	}
	if result.Loadable == nil || *result.Loadable {
		t.Fatalf("loadable should have been withdrawn: %v", result.Loadable)
	}
	if !hasCode(evidenceCodes(result), ReasonDrillStepsMissing) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
}

func TestPartialRankCoverageIsWithdrawn(t *testing.T) {
	drill := evidenceDrill()
	drill.RanksReported = []int{0}
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q", result.Status)
	}
	if result.Loadable == nil || *result.Loadable {
		t.Fatalf("loadable = %v", result.Loadable)
	}
	if !hasCode(evidenceCodes(result), ReasonDrillRankCoverageIncomplete) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
}

// The DoD states the acceptance case as "2 of 4": a drill that comes back with
// half the world is not a restorable checkpoint, and the verdict has to name
// the ranks the operator is missing instead of only counting them.
func TestHalfOfAFourRankWorldIsWithdrawn(t *testing.T) {
	drill := evidenceDrill()
	drill.RanksReported = []int{0, 1}
	drill.RanksExpected = 4
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(4),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q", result.Status)
	}
	if !hasCode(evidenceCodes(result), ReasonDrillRankCoverageIncomplete) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
	var detail string
	for _, reason := range result.Reasons {
		if reason.Code == ReasonDrillRankCoverageIncomplete {
			detail = reason.Detail
		}
	}
	if !strings.Contains(detail, "covered 2 of 4 ranks") ||
		!strings.Contains(detail, "missing ranks [2 3]") {
		t.Fatalf("detail = %q", detail)
	}
}

// DoD 5: a drill that names no rank has not proved coverage. Silence used to
// fall through the gate and leave loadable=true; it must now withdraw the
// claim rather than be read as "0 of 0, therefore fine".
func TestUnreportedCoverageIsWithdrawn(t *testing.T) {
	drill := evidenceDrill()
	drill.RanksReported = nil
	drill.RanksExpected = 0
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q (%v)", result.Status, evidenceCodes(result))
	}
	if result.Loadable == nil || *result.Loadable {
		t.Fatalf("a silent drill must not leave loadable=true: %v", result.Loadable)
	}
	if !hasCode(evidenceCodes(result), ReasonDrillRankCoverageUnreported) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
	if hasCode(evidenceCodes(result), ReasonDrillRankCoverageIncomplete) {
		t.Fatalf("silence must not be reported as a partial restore: %v",
			evidenceCodes(result))
	}
}

// A drill that names ranks but no world, against a record with no inventory,
// gives nothing to measure completeness against. Unproven is not satisfied.
func TestCoverageWithoutAWorldIsUnproven(t *testing.T) {
	drill := evidenceDrill()
	drill.RanksReported = []int{0}
	drill.RanksExpected = 0
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(0),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q", result.Status)
	}
	if !hasCode(evidenceCodes(result), ReasonDrillRankCoverageUnreported) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
}

func TestProofForAnotherCheckpointIsStaleNotFailed(t *testing.T) {
	cases := map[string]func(*DrillOutcome){
		"version": func(drill *DrillOutcome) { drill.CheckpointVersion = "step-99" },
		"digest": func(drill *DrillOutcome) {
			drill.CheckpointMetadataDigest = evidenceOtherDigest
		},
	}
	for name, mutate := range cases {
		mutate := mutate
		t.Run(name, func(t *testing.T) {
			drill := evidenceDrill()
			mutate(&drill)
			result := AssessReadiness(ReadinessInput{
				Record: evidenceRecord(2),
				Now:    evidenceNow(),
				Drill:  &drill,
			})
			codes := evidenceCodes(result)
			if result.Status != ReadinessTargetBlocked {
				t.Fatalf("status = %q", result.Status)
			}
			if !hasCode(codes, ReasonDrillProofStale) {
				t.Fatalf("reasons = %v", codes)
			}
			if hasCode(codes, ReasonDrillFailed) {
				t.Fatalf("a stale proof must not read as a failed drill: %v", codes)
			}
		})
	}
}

func TestManifestForAnotherCheckpointBlocks(t *testing.T) {
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  ptrDrill(evidenceDrill()),
		State: &StateClosureSummary{
			Status:            ClosureComplete,
			Closed:            true,
			CheckpointUID:     "job-other",
			CheckpointVersion: "step-100",
		},
		RequireStateComplete: true,
	})
	if result.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %q", result.Status)
	}
	if result.ReadinessLevel != "L3" {
		t.Fatalf("level = %q", result.ReadinessLevel)
	}
	if !hasCode(evidenceCodes(result), ReasonResumeManifestInvalid) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}
}

func TestWorldSizeChangeCarriesATopologyCaveat(t *testing.T) {
	target := 4
	result := AssessReadiness(ReadinessInput{
		Record:          evidenceRecord(2),
		Now:             evidenceNow(),
		Drill:           ptrDrill(evidenceDrill()),
		TargetWorldSize: &target,
	})
	if result.Status != ReadinessReady {
		t.Fatalf("status = %q", result.Status)
	}
	if result.Movable == nil || !*result.Movable {
		t.Fatalf("movable = %v", result.Movable)
	}
	if !hasCode(evidenceCodes(result), ReasonTargetTopologyUnverified) {
		t.Fatalf("reasons = %v", evidenceCodes(result))
	}

	verified := AssessReadiness(ReadinessInput{
		Record:           evidenceRecord(2),
		Now:              evidenceNow(),
		Drill:            ptrDrill(evidenceDrill()),
		TargetWorldSize:  &target,
		TopologyVerified: true,
	})
	if hasCode(evidenceCodes(verified), ReasonTargetTopologyUnverified) {
		t.Fatalf("a verified topology must drop the caveat: %v", evidenceCodes(verified))
	}
}

func ptrDrill(outcome DrillOutcome) *DrillOutcome {
	return &outcome
}

func TestMinDrillStepsIsOne(t *testing.T) {
	if MinDrillSteps != 1 {
		t.Fatalf("MinDrillSteps = %d", MinDrillSteps)
	}
	drill := evidenceDrill()
	drill.StepsRan = MinDrillSteps
	result := AssessReadiness(ReadinessInput{
		Record: evidenceRecord(2),
		Now:    evidenceNow(),
		Drill:  &drill,
	})
	if result.Status != ReadinessReady {
		t.Fatalf("status = %q (%v)", result.Status, evidenceCodes(result))
	}
}

func TestDrillEvidenceGapOrderingIsExplicit(t *testing.T) {
	// A drill can be wrong in several ways at once. The first gap wins, so the
	// reported reason is deterministic instead of map-order dependent.
	drill := evidenceDrill()
	drill.CheckpointMetadataDigest = evidenceOtherDigest
	drill.StepsRan = 0
	drill.RanksReported = nil
	gap, ok := drillEvidenceGap(evidenceRecord(2), drill)
	if !ok {
		t.Fatal("expected a gap")
	}
	if gap.Code != ReasonDrillProofStale {
		t.Fatalf("code = %q, want staleness first: %s", gap.Code, fmt.Sprint(gap.Detail))
	}
}
