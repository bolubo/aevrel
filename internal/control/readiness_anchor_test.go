package control

import (
	"testing"
	"time"
)

const (
	anchorDigest      = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
	anchorOtherDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)

func anchorInput() ReadinessInput {
	now := time.Date(2026, 10, 25, 12, 0, 0, 0, time.UTC)
	return ReadinessInput{
		Record: CheckpointRecord{
			Complete:    true,
			LatestMtime: now.Format(time.RFC3339),
		},
		Now: now,
		Drill: &DrillOutcome{
			Phase:                 "Succeeded",
			Succeeded:             true,
			StepsRan:              10,
			ResumeManifestDigests: []string{anchorDigest},
		},
		State: &StateClosureSummary{
			Status:         "COMPLETE",
			Closed:         true,
			ManifestDigest: anchorDigest,
		},
	}
}

func TestReadinessAcceptsMatchingManifestDigest(t *testing.T) {
	got := AssessReadiness(anchorInput())
	if got.Status != ReadinessReady {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessReady)
	}
	if got.Loadable == nil || !*got.Loadable {
		t.Fatalf("loadable = %v, want true", got.Loadable)
	}
	if got.LastDrillDigest != anchorDigest {
		t.Fatalf("last_drill_digest = %q", got.LastDrillDigest)
	}
}

func TestReadinessFailsClosedOnManifestDigestMismatch(t *testing.T) {
	input := anchorInput()
	input.Drill.ResumeManifestDigests = []string{anchorOtherDigest}
	got := AssessReadiness(input)
	if got.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessTargetBlocked)
	}
	if got.Loadable == nil || *got.Loadable {
		t.Fatalf("loadable = %v, want false", got.Loadable)
	}
	found := false
	for _, reason := range got.Reasons {
		if reason.Code != ReasonResumeDigestMismatch {
			continue
		}
		found = true
		if reason.Detail != "drill restored a different resume-state manifest" {
			t.Fatalf("detail = %q", reason.Detail)
		}
	}
	if !found {
		t.Fatalf("reasons = %+v", got.Reasons)
	}
}

func TestReadinessMismatchBlocksL4Claim(t *testing.T) {
	input := anchorInput()
	input.Drill.ResumeManifestDigests = []string{anchorOtherDigest}
	input.RequireStateComplete = true
	got := AssessReadiness(input)
	if got.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %s", got.Status)
	}
	if got.ReadinessLevel != "L3" {
		t.Fatalf("readiness level = %q, want L3", got.ReadinessLevel)
	}
}

func TestReadinessWithoutDigestEvidenceStaysReady(t *testing.T) {
	input := anchorInput()
	input.Drill.ResumeManifestDigests = nil
	got := AssessReadiness(input)
	if got.Status != ReadinessReady {
		t.Fatalf("status = %s", got.Status)
	}
	if got.LastDrillDigest != "" {
		t.Fatalf("last_drill_digest = %q", got.LastDrillDigest)
	}
}
