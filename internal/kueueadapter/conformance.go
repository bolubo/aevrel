package kueueadapter

import (
	"strings"

	"github.com/bolubo/aevrel/internal/control"
)

// Conformance violation codes. Each one names a way a fact set can contradict
// itself or claim more than it knows; none of them is ever silent.
const (
	ViolationFreshWithoutLoadability  = "FreshWithoutLoadabilityVerdict"
	ViolationRestoreWithoutProof      = "RestoreWithoutDrillProof"
	ViolationRTOWithoutComplete       = "RTOWithoutCompleteInventory"
	ViolationRTOZero                  = "RTOMustBePositiveOrNull"
	ViolationSchemaVersionMismatch    = "SchemaVersionMismatch"
	ViolationMissingSource            = "MissingSource"
	ViolationUnknownSource            = "UnknownFactSource"
	ViolationMissingObservedAt        = "MissingObservedAt"
	ViolationFallbackClaimsFacts      = "FallbackClaimsVerifiedFacts"
	ViolationFallbackClaimsRestore    = "FallbackClaimsRestoreMode"
	ViolationUnknownClaimsFacts       = "UnknownSourceClaimsFacts"
	ViolationAmbiguousRecordSelection = "AmbiguousRecordSelection"
)

// ConformanceReport is the verdict on one fact set. Valid means the envelope
// may be published; it never means the checkpoint is safe.
type ConformanceReport struct {
	Valid      bool     `json:"valid"`
	Violations []string `json:"violations,omitempty"`
}

// ValidateFacts checks a fact set before it is emitted. It answers one
// question: could a consumer read this as a schedulable statement without being
// misled? It does not predict what a consumer will do, and it never repairs the
// facts it is given.
//
// Three classes of violation are checked:
//
//  1. internal consistency, so a consumer cannot read two facts that contradict
//     each other;
//  2. self-description, so every fact set says when and where it came from;
//  3. no fabrication, so an unknown source never carries verified values and a
//     missing number is never published as a safe one.
//
// A fact set that fails any check is not projected onto labels at all. The
// binary protection decision is unaffected: it never reads facts.
func ValidateFacts(facts CheckpointFacts) ConformanceReport {
	violations := make([]string, 0, 4)

	// 1. Internal consistency.
	if facts.Fresh != nil && *facts.Fresh && facts.Loadable == nil {
		// A checkpoint cannot be fresh and simultaneously have no loadability
		// verdict: freshness alone is not schedulable evidence.
		violations = append(violations, ViolationFreshWithoutLoadability)
	}
	if facts.RestoreMode == string(control.RescueRestore) && facts.LastDrill == "" {
		violations = append(violations, ViolationRestoreWithoutProof)
	}
	if facts.EstimatedRTOSeconds != nil &&
		(facts.Complete == nil || !*facts.Complete) {
		violations = append(violations, ViolationRTOWithoutComplete)
	}

	// 2. Self-description.
	if facts.SchemaVersion != SchemaVersion {
		violations = append(violations, ViolationSchemaVersionMismatch)
	}
	source := strings.TrimSpace(facts.Source)
	if source == "" {
		violations = append(violations, ViolationMissingSource)
	}
	switch source {
	case FactSourceCheckpointRecord:
		if strings.TrimSpace(facts.ObservedAt) == "" {
			violations = append(violations, ViolationMissingObservedAt)
		}
	case FactSourceCheckpointRecordAmbiguous,
		FactSourceAnnotationFallback,
		FactSourceUnknown:
		// These sources carry no observation by definition.
	default:
		if source != "" {
			violations = append(violations, ViolationUnknownSource)
		}
	}

	// 3. No fabrication.
	switch source {
	case FactSourceAnnotationFallback:
		if factsCarryVerifiedValues(facts) {
			violations = append(violations, ViolationFallbackClaimsFacts)
		}
		if facts.RestoreMode != string(control.RescueHold) {
			violations = append(violations, ViolationFallbackClaimsRestore)
		}
	case FactSourceUnknown:
		if factsCarryVerifiedValues(facts) {
			violations = append(violations, ViolationUnknownClaimsFacts)
		}
	}
	if facts.EstimatedRTOSeconds != nil && *facts.EstimatedRTOSeconds <= 0 {
		// 0 is not a known RTO; it is the most dangerous possible default.
		violations = append(violations, ViolationRTOZero)
	}
	if facts.Ambiguous {
		violations = append(violations, ViolationAmbiguousRecordSelection)
	}

	return ConformanceReport{
		Valid:      len(violations) == 0,
		Violations: violations,
	}
}

func factsCarryVerifiedValues(facts CheckpointFacts) bool {
	return facts.Complete != nil ||
		facts.Fresh != nil ||
		facts.Loadable != nil ||
		facts.Movable != nil ||
		facts.EstimatedRTOSeconds != nil ||
		strings.TrimSpace(facts.ObservedAt) != "" ||
		len(facts.LastDrill) > 0 ||
		len(facts.ReasonCodes) > 0
}
