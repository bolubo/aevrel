package control

import (
	"testing"
	"time"
)

func TestReadinessRequiresDrill(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	record := CheckpointRecord{
		Complete:    true,
		LatestMtime: now.Add(-time.Minute).Format(time.RFC3339),
	}
	got := AssessReadiness(ReadinessInput{Record: record, Now: now})
	if got.Status != ReadinessUnverified {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessUnverified)
	}
	if got.Loadable != nil {
		t.Fatalf("loadable = %v, want nil", got.Loadable)
	}
}

func TestReadinessReadyWithDrill(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	target := 3
	success := DrillOutcome{
		Phase:         "Succeeded",
		Succeeded:     true,
		StepsRan:      10,
		ProofRef:      "proof://drill/1",
		RanksReported: []int{0, 1, 2},
		RanksExpected: 3,
	}
	record := CheckpointRecord{
		Complete:    true,
		LatestMtime: now.Add(-time.Minute).Format(time.RFC3339),
	}
	got := AssessReadiness(ReadinessInput{Record: record, Now: now, TargetWorldSize: &target, Drill: &success})
	if got.Status != ReadinessReady {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessReady)
	}
	if got.Loadable == nil || !*got.Loadable {
		t.Fatalf("loadable = %v, want true", got.Loadable)
	}
	if got.Movable == nil || !*got.Movable {
		t.Fatalf("movable = %v, want true", got.Movable)
	}
}

func TestReadinessIncompleteWinsOverStale(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	record := CheckpointRecord{
		Complete:    false,
		LatestMtime: now.Add(-48 * time.Hour).Format(time.RFC3339),
		Reasons: []Reason{{
			Code:   ReasonRankMissing,
			Detail: "rank 2 missing",
		}},
	}
	got := AssessReadiness(ReadinessInput{Record: record, Now: now, MaxAge: time.Minute})
	if got.Status != ReadinessIncomplete {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessIncomplete)
	}
}

func TestReadinessFailedDrillBlocks(t *testing.T) {
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.UTC)
	record := CheckpointRecord{
		Complete:    true,
		LatestMtime: now.Add(-time.Minute).Format(time.RFC3339),
	}
	failed := DrillOutcome{
		Phase:     "Failed",
		Succeeded: false,
		Detail:    "target load failed",
	}
	got := AssessReadiness(ReadinessInput{Record: record, Now: now, Drill: &failed})
	if got.Status != ReadinessTargetBlocked {
		t.Fatalf("status = %s, want %s", got.Status, ReadinessTargetBlocked)
	}
}
