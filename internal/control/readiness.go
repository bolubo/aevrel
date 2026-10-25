package control

import (
	"fmt"
	"time"
)

const DefaultMaxAge = 24 * time.Hour

// StateClosureSummary is the controller-side view of a resume-state
// closure report (RFC #30). Only the closure verdict and its specific
// reason codes cross the language boundary.
// ClosureComplete is the only closure verdict that may carry an L4
// claim; every other verdict means at least one component is unproven.
const ClosureComplete = "COMPLETE"

type StateClosureSummary struct {
	Status string
	Closed bool
	Codes  []string
	// ManifestDigest is the digest of the manifest this verdict describes.
	// Comparing it with the digest a drill restored anchors both claims to
	// the same checkpoint state.
	ManifestDigest string
}

// ReadinessInput is everything the assessment needs. New inputs are added
// here instead of growing the parameter list.
type ReadinessInput struct {
	Record          CheckpointRecord
	Now             time.Time
	MaxAge          time.Duration // zero means DefaultMaxAge
	TargetWorldSize *int
	Drill           *DrillOutcome
	State           *StateClosureSummary
	// RequireStateComplete turns the assessment into an L4 claim: the
	// result may not be READY unless the resume-state closure is complete.
	RequireStateComplete bool
}

// AssessReadiness is the single readiness entry point. It implements
// docs/READINESS-SEMANTICS.md.
func AssessReadiness(input ReadinessInput) CheckpointReadiness {
	record := input.Record
	now := input.Now
	maxAge := input.MaxAge
	if maxAge <= 0 {
		maxAge = DefaultMaxAge
	}
	reasons := append([]Reason(nil), record.Reasons...)
	fresh := false
	var ageSeconds *int64
	if latest, ok := contentTimestamp(record); ok {
		seconds := int64(now.Sub(latest).Seconds())
		if seconds < 0 {
			seconds = 0
		}
		ageSeconds = &seconds
		fresh = now.Sub(latest) <= maxAge
		if !fresh {
			reasons = append(reasons, Reason{
				Code:   ReasonStale,
				Detail: fmt.Sprintf("checkpoint is %d seconds old", seconds),
			})
		}
	}

	var movable *bool
	if input.TargetWorldSize != nil {
		if *input.TargetWorldSize < 1 {
			reasons = append(reasons, Reason{
				Code:   ReasonTargetBlocked,
				Detail: "target world size must be positive",
			})
		}
		value := record.Complete && *input.TargetWorldSize > 0
		movable = &value
		if !value {
			reasons = append(reasons, Reason{
				Code:   ReasonTargetBlocked,
				Detail: "source is incomplete or target is invalid",
			})
		}
	}

	var loadable *bool
	lastDrill := ""
	lastDrillDigest := ""
	if input.Drill != nil && len(input.Drill.ResumeManifestDigests) == 1 {
		lastDrillDigest = input.Drill.ResumeManifestDigests[0]
	}
	if input.Drill != nil {
		lastDrill = input.Drill.ProofRef
		if lastDrill == "" {
			lastDrill = input.Drill.Phase
		}
		value := input.Drill.Succeeded
		loadable = &value
		if !value {
			detail := input.Drill.Detail
			if detail == "" {
				detail = "restore drill failed"
			}
			reasons = append(reasons, Reason{
				Code:   ReasonDrillFailed,
				Detail: detail,
			})
		}
	} else {
		reasons = append(reasons, Reason{
			Code:   ReasonDrillMissing,
			Detail: "no restore drill has verified this checkpoint",
		})
	}

	var stateComplete *bool
	closureGateBlocked := false
	if input.State != nil {
		value := input.State.Closed
		stateComplete = &value
		if input.RequireStateComplete && !value {
			closureGateBlocked = true
			blocked := false
			loadable = &blocked
			reasons = append(reasons, Reason{
				Code:   ReasonStateClosureIncomplete,
				Detail: closureDetail(input.State),
			})
		}
	}

	// Anchor: the manifest the drill restored must be the manifest the
	// closure verdict describes.
	anchorBlocked := false
	if input.Drill != nil && input.State != nil && input.State.ManifestDigest != "" {
		for _, digest := range input.Drill.ResumeManifestDigests {
			if digest == input.State.ManifestDigest {
				continue
			}
			anchorBlocked = true
			value := false
			loadable = &value
			reasons = append(reasons, Reason{
				Code:   ReasonResumeDigestMismatch,
				Detail: "drill restored a different resume-state manifest",
			})
			break
		}
	}

	status := ReadinessReady
	switch {
	case !record.Complete:
		status = ReadinessIncomplete
	case !fresh:
		status = ReadinessStale
	case input.Drill != nil && !input.Drill.Succeeded:
		status = ReadinessTargetBlocked
	case anchorBlocked:
		status = ReadinessTargetBlocked
	case loadable == nil:
		status = ReadinessUnverified
	case movable != nil && !*movable:
		status = ReadinessTargetBlocked
	}
	if closureGateBlocked && status == ReadinessReady {
		status = ReadinessTargetBlocked
	}

	// L4 is a claim, so it requires the gate to be applied, the closure to
	// be complete, the whole assessment to end READY, and the drill to have
	// proved it restored *this* manifest. A complete closure nobody resumed
	// from is an L3 fact, not an L4 proof.
	readinessLevel := "L3"
	if input.RequireStateComplete &&
		stateComplete != nil &&
		*stateComplete &&
		lastDrillDigest != "" &&
		status == ReadinessReady {
		readinessLevel = "L4"
	}

	return CheckpointReadiness{
		Status:              status,
		Complete:            record.Complete,
		Fresh:               fresh,
		Loadable:            loadable,
		Movable:             movable,
		StateComplete:       stateComplete,
		ReadinessLevel:      readinessLevel,
		Reasons:             dedupeReasons(reasons),
		ObservedAt:          now.UTC().Format(time.RFC3339),
		TargetWorldSize:     input.TargetWorldSize,
		EstimatedRTOSeconds: ageSeconds,
		LastDrill:           lastDrill,
		LastDrillDigest:     lastDrillDigest,
	}
}

// contentTimestamp returns the newest timestamp that describes the
// checkpoint content itself. Provider-declared CreatedAt wins; the
// filesystem LatestMtime is a fallback because copies and object stores
// may rewrite it. The scan time ObservedAt is deliberately excluded.
func contentTimestamp(record CheckpointRecord) (time.Time, bool) {
	if record.CreatedAt != "" {
		if parsed, err := time.Parse(time.RFC3339, record.CreatedAt); err == nil {
			return parsed, true
		}
	}
	if record.LatestMtime != "" {
		if parsed, err := time.Parse(time.RFC3339, record.LatestMtime); err == nil {
			return parsed, true
		}
	}
	return time.Time{}, false
}

func closureDetail(state *StateClosureSummary) string {
	detail := fmt.Sprintf("resume-state closure is %s", state.Status)
	for index, code := range state.Codes {
		if index == 0 {
			detail += ": "
		} else {
			detail += ", "
		}
		detail += code
	}
	return detail
}

func dedupeReasons(reasons []Reason) []Reason {
	seen := make(map[string]struct{}, len(reasons))
	result := make([]Reason, 0, len(reasons))
	for _, reason := range reasons {
		key := reason.Code + "\x00" + reason.Detail
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}
		result = append(result, reason)
	}
	return result
}
