package kueueadapter

import (
	"testing"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func validRecordFacts() CheckpointFacts {
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:              aevrelv1.ReadinessReady,
		Complete:            true,
		Fresh:               true,
		Loadable:            factBoolPtr(true),
		ObservedAt:          factTestObservedAt,
		LastDrill:           "default/training-drill",
		LastDrillVersion:    "step-100",
		LastDrillDigest:     "sha256:abc",
		EstimatedRTOSeconds: int64Ptr(120),
	}
	return CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
}

func TestValidateFactsAcceptsAConsistentRecord(t *testing.T) {
	facts := validRecordFacts()
	if !facts.Conformance.Valid {
		t.Fatalf("conformance = %+v", facts.Conformance)
	}
	if len(facts.Conformance.Violations) != 0 {
		t.Fatalf("violations = %v", facts.Conformance.Violations)
	}
}

func TestValidateFactsInternalConsistency(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*CheckpointFacts)
		want   string
	}{
		{
			name: "fresh without a loadability verdict",
			mutate: func(facts *CheckpointFacts) {
				facts.Loadable = nil
			},
			want: ViolationFreshWithoutLoadability,
		},
		{
			name: "restore without a drill proof",
			mutate: func(facts *CheckpointFacts) {
				facts.LastDrill = ""
			},
			want: ViolationRestoreWithoutProof,
		},
		{
			// A drill name is a reference, not evidence: without the digest
			// the proof is not bound to any checkpoint content, so it cannot
			// be invalidated when the checkpoint changes.
			name: "restore with an unbound drill reference",
			mutate: func(facts *CheckpointFacts) {
				facts.LastDrillDigest = ""
			},
			want: ViolationRestoreWithoutProof,
		},
		{
			name: "RTO without a complete inventory",
			mutate: func(facts *CheckpointFacts) {
				facts.Complete = factBoolPtr(false)
			},
			want: ViolationRTOWithoutComplete,
		},
	}
	for _, testCase := range cases {
		facts := validRecordFacts()
		testCase.mutate(&facts)
		report := ValidateFacts(facts)
		if report.Valid {
			t.Fatalf("%s: expected an invalid fact set", testCase.name)
		}
		if !containsString(report.Violations, testCase.want) {
			t.Fatalf(
				"%s: violations = %v, want %q",
				testCase.name,
				report.Violations,
				testCase.want,
			)
		}
	}
}

func TestValidateFactsSelfDescription(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*CheckpointFacts)
		want   string
	}{
		{
			name: "wrong schema version",
			mutate: func(facts *CheckpointFacts) {
				facts.SchemaVersion = "aevrel.dev/v2"
			},
			want: ViolationSchemaVersionMismatch,
		},
		{
			name: "missing source",
			mutate: func(facts *CheckpointFacts) {
				facts.Source = ""
			},
			want: ViolationMissingSource,
		},
		{
			name: "unknown source",
			mutate: func(facts *CheckpointFacts) {
				facts.Source = "made-up-source"
			},
			want: ViolationUnknownSource,
		},
		{
			name: "record without an observation time",
			mutate: func(facts *CheckpointFacts) {
				facts.ObservedAt = ""
			},
			want: ViolationMissingObservedAt,
		},
	}
	for _, testCase := range cases {
		facts := validRecordFacts()
		testCase.mutate(&facts)
		report := ValidateFacts(facts)
		if report.Valid {
			t.Fatalf("%s: expected an invalid fact set", testCase.name)
		}
		if !containsString(report.Violations, testCase.want) {
			t.Fatalf(
				"%s: violations = %v, want %q",
				testCase.name,
				report.Violations,
				testCase.want,
			)
		}
	}
}

func TestValidateFactsNeverAcceptsFabricatedValues(t *testing.T) {
	cases := []struct {
		name   string
		base   func() CheckpointFacts
		mutate func(*CheckpointFacts)
		want   string
	}{
		{
			name: "annotation fallback claims verified facts",
			base: func() CheckpointFacts {
				return CheckpointFactsForAnnotations(false, FactsOptions{})
			},
			mutate: func(facts *CheckpointFacts) {
				facts.Complete = factBoolPtr(true)
			},
			want: ViolationFallbackClaimsFacts,
		},
		{
			name: "annotation fallback claims a restore mode",
			base: func() CheckpointFacts {
				return CheckpointFactsForAnnotations(false, FactsOptions{})
			},
			mutate: func(facts *CheckpointFacts) {
				facts.RestoreMode = "restore"
			},
			want: ViolationFallbackClaimsRestore,
		},
		{
			name: "unknown source claims facts",
			base: func() CheckpointFacts {
				return CheckpointFactsForNoSignal(FactsOptions{})
			},
			mutate: func(facts *CheckpointFacts) {
				facts.Fresh = factBoolPtr(true)
			},
			want: ViolationUnknownClaimsFacts,
		},
		{
			name: "zero RTO is not a known value",
			base: validRecordFacts,
			mutate: func(facts *CheckpointFacts) {
				facts.EstimatedRTOSeconds = int64Ptr(0)
			},
			want: ViolationRTOZero,
		},
		{
			name: "ambiguous selection is never publishable",
			base: validRecordFacts,
			mutate: func(facts *CheckpointFacts) {
				facts.Ambiguous = true
			},
			want: ViolationAmbiguousRecordSelection,
		},
	}
	for _, testCase := range cases {
		facts := testCase.base()
		testCase.mutate(&facts)
		report := ValidateFacts(facts)
		if report.Valid {
			t.Fatalf("%s: expected an invalid fact set", testCase.name)
		}
		if !containsString(report.Violations, testCase.want) {
			t.Fatalf(
				"%s: violations = %v, want %q",
				testCase.name,
				report.Violations,
				testCase.want,
			)
		}
	}
}

func TestValidateFactsKeepsUnverifiedRecordPublishable(t *testing.T) {
	// A record with no drill is a legitimate state: it is not self-contradictory
	// as long as freshness is not claimed alongside a missing verdict.
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessUnverified,
		Complete:   true,
		Fresh:      false,
		ObservedAt: factTestObservedAt,
	}
	facts := CheckpointFactsForRecord(
		record,
		1,
		"training-checkpoint",
		FactsOptions{Now: factTestNow()},
	)
	if !facts.Conformance.Valid {
		t.Fatalf("conformance = %+v", facts.Conformance)
	}
	if facts.RestoreMode != "hold" {
		t.Fatalf("restoreMode = %q", facts.RestoreMode)
	}
}
