package kueueadapter

import (
	"testing"
	"time"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

const factTestObservedAt = "2026-10-25T12:00:00Z"

func factTestNow() time.Time {
	observed, err := time.Parse(time.RFC3339, factTestObservedAt)
	if err != nil {
		panic(err)
	}
	return observed
}

func factTestRecord() *aevrelv1.CheckpointRecord {
	return &aevrelv1.CheckpointRecord{
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:  "training",
			Version: "step-100",
		},
	}
}

func TestCheckpointFactsMirrorReadiness(t *testing.T) {
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:              aevrelv1.ReadinessReady,
		Complete:            true,
		Fresh:               true,
		Loadable:            factBoolPtr(true),
		Movable:             factBoolPtr(false),
		ObservedAt:          factTestObservedAt,
		LastDrill:           "default/training-drill",
		LastDrillVersion:    "step-100",
		LastDrillDigest:     "sha256:abc",
		TargetWorldSize:     factInt32Ptr(8),
		EstimatedRTOSeconds: int64Ptr(90),
		StateComplete:       factBoolPtr(true),
		ReadinessLevel:      "L4",
	}

	facts := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)

	if facts.SchemaVersion != SchemaVersion {
		t.Fatalf("schemaVersion = %q", facts.SchemaVersion)
	}
	if facts.Source != FactSourceCheckpointRecord {
		t.Fatalf("source = %q", facts.Source)
	}
	if facts.Status != aevrelv1.ReadinessReady {
		t.Fatalf("status = %q", facts.Status)
	}
	if facts.Complete == nil || !*facts.Complete {
		t.Fatalf("complete = %v", facts.Complete)
	}
	if facts.Fresh == nil || !*facts.Fresh {
		t.Fatalf("fresh = %v", facts.Fresh)
	}
	if facts.Loadable == nil || !*facts.Loadable {
		t.Fatalf("loadable = %v", facts.Loadable)
	}
	if facts.Movable == nil || *facts.Movable {
		t.Fatalf("movable = %v", facts.Movable)
	}
	if facts.ObservedAt != factTestObservedAt {
		t.Fatalf("observedAt = %q", facts.ObservedAt)
	}
	if facts.EstimatedRTOSeconds == nil || *facts.EstimatedRTOSeconds != 90 {
		t.Fatalf("estimatedRTOSeconds = %v", facts.EstimatedRTOSeconds)
	}
	if facts.SelectedRecord != "training-checkpoint" || facts.Candidates != 1 {
		t.Fatalf(
			"selection = %q/%d",
			facts.SelectedRecord,
			facts.Candidates,
		)
	}
	if facts.LastDrillVersion != "step-100" || facts.LastDrillDigest != "sha256:abc" {
		t.Fatalf("drill fields = %q/%q", facts.LastDrillVersion, facts.LastDrillDigest)
	}
	if facts.StateComplete == nil || !*facts.StateComplete {
		t.Fatalf("stateComplete = %v", facts.StateComplete)
	}
	if facts.ReadinessLevel != "L4" {
		t.Fatalf("readinessLevel = %q", facts.ReadinessLevel)
	}
	if facts.TargetWorldSize == nil || *facts.TargetWorldSize != 8 {
		t.Fatalf("targetWorldSize = %v", facts.TargetWorldSize)
	}
	if facts.RestoreMode != "restore" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
	if !facts.Conformance.Valid {
		t.Fatalf("conformance = %+v", facts.Conformance)
	}
}

func TestCheckpointFactsInventNothingWhenReadinessIsEmpty(t *testing.T) {
	facts := CheckpointFactsForRecord(
		factTestRecord(),
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	if facts.Loadable != nil {
		t.Fatalf("loadable must stay unknown, got %v", facts.Loadable)
	}
	if facts.Movable != nil {
		t.Fatalf("movable must stay unknown, got %v", facts.Movable)
	}
	if facts.EstimatedRTOSeconds != nil {
		t.Fatalf(
			"estimatedRTOSeconds must stay unknown, got %v",
			*facts.EstimatedRTOSeconds,
		)
	}
	if facts.Complete == nil || *facts.Complete {
		t.Fatalf("complete = %v", facts.Complete)
	}
	if facts.Fresh == nil || *facts.Fresh {
		t.Fatalf("fresh = %v", facts.Fresh)
	}
	if facts.RestoreMode != "hold" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
	if facts.ReasonCodes == nil {
		t.Fatal("reasonCodes must be an empty slice, not nil")
	}
}

func TestCheckpointFactsRestoreModeComesFromDecideRescue(t *testing.T) {
	ready := aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessReady,
		Complete:   true,
		Fresh:      true,
		Loadable:   factBoolPtr(true),
		ObservedAt: factTestObservedAt,
		LastDrill:  "default/training-drill",
	}
	cases := []struct {
		name       string
		readiness  aevrelv1.CheckpointReadinessStatus
		wantMode   string
		wantReason string
	}{
		{
			name:       "ready restores",
			readiness:  ready,
			wantMode:   "restore",
			wantReason: "RESTORE_READY",
		},
		{
			name: "unverified without a drill holds",
			readiness: aevrelv1.CheckpointReadinessStatus{
				Status:     aevrelv1.ReadinessUnverified,
				Complete:   true,
				Fresh:      true,
				ObservedAt: factTestObservedAt,
			},
			wantMode:   "hold",
			wantReason: "DRILL_MISSING",
		},
		{
			name: "stale holds",
			readiness: aevrelv1.CheckpointReadinessStatus{
				Status:     aevrelv1.ReadinessStale,
				Complete:   true,
				Fresh:      false,
				Loadable:   factBoolPtr(true),
				ObservedAt: factTestObservedAt,
				LastDrill:  "default/training-drill",
			},
			wantMode:   "hold",
			wantReason: "CHECKPOINT_STALE",
		},
		{
			name: "incomplete holds",
			readiness: aevrelv1.CheckpointReadinessStatus{
				Status:     aevrelv1.ReadinessIncomplete,
				ObservedAt: factTestObservedAt,
			},
			wantMode:   "hold",
			wantReason: "CHECKPOINT_INCOMPLETE",
		},
	}
	for _, testCase := range cases {
		record := factTestRecord()
		record.Status.Readiness = testCase.readiness
		facts := CheckpointFactsForRecord(
			record,
			1,
			"training-checkpoint",
			FactsOptions{Now: factTestNow()},
		)
		if facts.RestoreMode != testCase.wantMode {
			t.Fatalf(
				"%s: restoreMode = %q, want %q",
				testCase.name,
				facts.RestoreMode,
				testCase.wantMode,
			)
		}
		if facts.RestoreReasonCode != testCase.wantReason {
			t.Fatalf(
				"%s: restoreReasonCode = %q, want %q",
				testCase.name,
				facts.RestoreReasonCode,
				testCase.wantReason,
			)
		}
	}
}

func TestCheckpointFactsCarryReadinessReasonCodesVerbatim(t *testing.T) {
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessStale,
		Complete:   true,
		Fresh:      false,
		ObservedAt: factTestObservedAt,
		Reasons: []aevrelv1.ControlReason{
			{Code: "STALE", Detail: "content is older than the window"},
			{Code: "RANK_MISSING", Detail: "rank 3 was never observed"},
		},
	}
	facts := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	want := []string{"STALE", "RANK_MISSING"}
	if len(facts.ReasonCodes) != len(want) {
		t.Fatalf("reasonCodes = %v", facts.ReasonCodes)
	}
	for index, code := range want {
		if facts.ReasonCodes[index] != code {
			t.Fatalf("reasonCodes[%d] = %q, want %q", index, facts.ReasonCodes[index], code)
		}
	}
}

func TestCheckpointFactsForAmbiguityPublishesNoFacts(t *testing.T) {
	facts := CheckpointFactsForAmbiguity(3, FactsOptions{Now: factTestNow()})
	if facts.Source != FactSourceCheckpointRecordAmbiguous {
		t.Fatalf("source = %q", facts.Source)
	}
	if !facts.Ambiguous || facts.Candidates != 3 {
		t.Fatalf("ambiguity = %v/%d", facts.Ambiguous, facts.Candidates)
	}
	if facts.Loadable != nil || facts.Complete != nil || facts.Fresh != nil {
		t.Fatalf("ambiguous facts carry values: %+v", facts)
	}
	if facts.RestoreMode != "hold" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
	if facts.Conformance.Valid {
		t.Fatal("an ambiguous fact set must not be publishable")
	}
}

func TestCheckpointFactsAnnotationFallbackCarriesNoVerifiedValues(t *testing.T) {
	facts := CheckpointFactsForAnnotations(false, FactsOptions{Now: factTestNow()})
	if facts.Source != FactSourceAnnotationFallback {
		t.Fatalf("source = %q", facts.Source)
	}
	if facts.Complete != nil || facts.Fresh != nil ||
		facts.Loadable != nil || facts.Movable != nil {
		t.Fatalf("fallback facts carry verified values: %+v", facts)
	}
	if facts.EstimatedRTOSeconds != nil {
		t.Fatalf("fallback facts carry an RTO: %v", *facts.EstimatedRTOSeconds)
	}
	if facts.RestoreMode != "hold" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
	if !facts.Conformance.Valid {
		t.Fatalf("conformance = %+v", facts.Conformance)
	}

	invalid := CheckpointFactsForAnnotations(true, FactsOptions{Now: factTestNow()})
	if !containsString(invalid.EnvelopeReasons, FactReasonAnnotationInvalid) {
		t.Fatalf("envelopeReasons = %v", invalid.EnvelopeReasons)
	}
}

func TestCheckpointFactsForNoSignalSaysSo(t *testing.T) {
	facts := CheckpointFactsForNoSignal(FactsOptions{Now: factTestNow()})
	if facts.Source != FactSourceUnknown {
		t.Fatalf("source = %q", facts.Source)
	}
	if !containsString(facts.EnvelopeReasons, FactReasonNoCheckpointRecord) {
		t.Fatalf("envelopeReasons = %v", facts.EnvelopeReasons)
	}
	if facts.RestoreMode != "hold" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
	if !facts.Conformance.Valid {
		t.Fatalf("conformance = %+v", facts.Conformance)
	}
}

func TestCheckpointFactsStaleObservationOnlyDowngrades(t *testing.T) {
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessReady,
		Complete:   true,
		Fresh:      true,
		Loadable:   factBoolPtr(true),
		ObservedAt: factTestObservedAt,
		LastDrill:  "default/training-drill",
	}

	// Baseline: no age limit, so the control plane's freshness stands.
	baseline := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	if baseline.Fresh == nil || !*baseline.Fresh {
		t.Fatalf("baseline fresh = %v", baseline.Fresh)
	}
	if baseline.RestoreMode != "restore" {
		t.Fatalf("baseline restoreMode = %q", baseline.RestoreMode)
	}

	// Two hours later the observation cannot still be called fresh.
	stale := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{
			Now:               factTestNow().Add(2 * time.Hour),
			MaxObservationAge: 30 * time.Minute,
		},
	)
	if stale.Fresh == nil || *stale.Fresh {
		t.Fatalf("stale fresh = %v", stale.Fresh)
	}
	if stale.RestoreMode != "hold" {
		t.Fatalf("stale restoreMode = %q", stale.RestoreMode)
	}
	if stale.RestoreReasonCode != "CHECKPOINT_STALE" {
		t.Fatalf("stale restoreReasonCode = %q", stale.RestoreReasonCode)
	}
	if !containsString(stale.EnvelopeReasons, FactReasonStaleObservation) {
		t.Fatalf("envelopeReasons = %v", stale.EnvelopeReasons)
	}
	if len(stale.ReasonCodes) != 0 {
		t.Fatalf("the envelope must not invent readiness reasons: %v", stale.ReasonCodes)
	}
	if !stale.Conformance.Valid {
		t.Fatalf("conformance = %+v", stale.Conformance)
	}
}

func TestCheckpointFactsAreDeterministicForOneRecord(t *testing.T) {
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessReady,
		Complete:   true,
		Fresh:      true,
		Loadable:   factBoolPtr(true),
		ObservedAt: factTestObservedAt,
		LastDrill:  "default/training-drill",
	}
	first := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	second := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	if first.RestoreMode != second.RestoreMode ||
		first.Conformance.Valid != second.Conformance.Valid ||
		first.Source != second.Source {
		t.Fatalf("facts are not deterministic: %+v vs %+v", first, second)
	}
}

func factInt32Ptr(value int32) *int32 {
	return &value
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
