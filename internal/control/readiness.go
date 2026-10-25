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
	// CheckpointUID, CheckpointVersion and CheckpointMetadataDigest are the
	// identity the manifest claims for itself. A manifest that names another
	// checkpoint cannot vouch for this one, no matter how complete it is.
	CheckpointUID            string
	CheckpointVersion        string
	CheckpointMetadataDigest string
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
	// TopologyVerified says a caller has checked mesh, placement and format
	// compatibility for the requested target. Without it, movable=true only
	// means "the source is complete and the target world size is sane", and
	// the assessment says so instead of implying more.
	TopologyVerified bool
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
	evidenceBlocked := false
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
		} else if gap, ok := drillEvidenceGap(record, *input.Drill); ok {
			// A drill that reports success still has to prove that it ran
			// training steps, covered the whole world, and was produced for
			// this checkpoint. Anything less withdraws the claim instead of
			// rounding it up to loadable=true.
			evidenceBlocked = true
			withdrawn := false
			loadable = &withdrawn
			reasons = append(reasons, gap)
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

	// Anchor 1: the manifest must describe *this* checkpoint. A manifest for
	// another job, version or metadata digest would let a complete closure
	// vouch for state that is not on disk here.
	manifestBlocked := false
	if input.State != nil {
		if detail := manifestIdentityGap(record, *input.State); detail != "" {
			manifestBlocked = true
			value := false
			loadable = &value
			reasons = append(reasons, Reason{
				Code:   ReasonResumeManifestInvalid,
				Detail: detail,
			})
		}
	}

	// Anchor 2: the manifest the drill restored must be the manifest the
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

	// movable=true means the source is complete and the target world size is
	// sane. It is not a topology verdict. The caveat is attached only when the
	// target actually changes the world size, because that is precisely the
	// case where the coarse check is doing the most work and a consumer is
	// most likely to over-read it. Emitting it on every assessment would make
	// the reason list permanently noisy and train consumers to ignore it.
	if movable != nil && *movable && !input.TopologyVerified &&
		input.TargetWorldSize != nil &&
		len(record.Ranks) > 0 &&
		*input.TargetWorldSize != len(record.Ranks) {
		reasons = append(reasons, Reason{
			Code: ReasonTargetTopologyUnverified,
			Detail: fmt.Sprintf(
				"target world size %d differs from the source's %d ranks; "+
					"mesh, placement and format compatibility are unverified",
				*input.TargetWorldSize,
				len(record.Ranks),
			),
		})
	}

	status := ReadinessReady
	switch {
	case !record.Complete:
		status = ReadinessIncomplete
	case !fresh:
		status = ReadinessStale
	case input.Drill != nil && !input.Drill.Succeeded:
		status = ReadinessTargetBlocked
	case evidenceBlocked || anchorBlocked || manifestBlocked:
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

// drillEvidenceGap reports why a drill that calls itself successful still
// cannot support loadable=true. The check is deliberately one-directional: it
// can only withdraw a claim, never create one.
func drillEvidenceGap(record CheckpointRecord, drill DrillOutcome) (Reason, bool) {
	if drill.CheckpointMetadataDigest != "" &&
		record.MetadataDigest != "" &&
		drill.CheckpointMetadataDigest != record.MetadataDigest {
		return Reason{
			Code: ReasonDrillProofStale,
			Detail: fmt.Sprintf(
				"drill proof is bound to metadata digest %q, the record is %q",
				drill.CheckpointMetadataDigest,
				record.MetadataDigest,
			),
		}, true
	}
	if drill.CheckpointVersion != "" &&
		record.Version != "" &&
		drill.CheckpointVersion != record.Version {
		return Reason{
			Code: ReasonDrillProofStale,
			Detail: fmt.Sprintf(
				"drill proof is bound to version %q, the record is %q",
				drill.CheckpointVersion,
				record.Version,
			),
		}, true
	}
	if drill.StepsRan < MinDrillSteps {
		return Reason{
			Code: ReasonDrillStepsMissing,
			Detail: fmt.Sprintf(
				"restore drill ran %d training steps, at least %d are required",
				drill.StepsRan,
				MinDrillSteps,
			),
		}, true
	}
	return rankCoverageGap(record, drill)
}

// rankCoverageGap reports why the drill's rank coverage does not support
// loadable=true.
//
// Three answers, and they are deliberately different facts:
//
//   - the drill named no rank: coverage is *unreported*
//     (DRILL_RANK_COVERAGE_UNREPORTED). Absence is not zero, and it is
//     certainly not "all ranks". This branch is checked first on purpose:
//     measuring a silent drill against the inventory would report "covered
//     0 of 4", which asserts that the drill ran and proved nothing — a fact
//     we do not have;
//   - the drill covered fewer ranks than the checkpoint has: the restore was
//     partial (DRILL_RANK_COVERAGE_INCOMPLETE), and the missing ranks are
//     named so an operator can act on them;
//   - the drill covered every expected rank: no gap.
func rankCoverageGap(record CheckpointRecord, drill DrillOutcome) (Reason, bool) {
	if len(drill.RanksReported) == 0 {
		return Reason{
			Code: ReasonDrillRankCoverageUnreported,
			Detail: "restore drill reported no rank coverage, " +
				"so full coverage is unproven",
		}, true
	}
	reported := make(map[int]struct{}, len(drill.RanksReported))
	for _, rank := range drill.RanksReported {
		reported[rank] = struct{}{}
	}
	// The checkpoint's own rank inventory is authoritative when present: a
	// drill can only be judged against the world the checkpoint actually
	// has. RanksExpected is the fallback for records that carry no
	// per-rank inventory.
	expected := make([]int, 0, len(record.Ranks))
	for _, inventory := range record.Ranks {
		expected = append(expected, inventory.Rank)
	}
	if len(expected) == 0 {
		for rank := 0; rank < drill.RanksExpected; rank++ {
			expected = append(expected, rank)
		}
	}
	if len(expected) == 0 {
		// The drill named ranks but no world size, and the record carries no
		// inventory: there is nothing to measure completeness against, so
		// the claim stays unproven instead of being rounded up.
		return Reason{
			Code: ReasonDrillRankCoverageUnreported,
			Detail: fmt.Sprintf(
				"restore drill reported ranks %v but no expected world size, "+
					"so full coverage is unproven",
				drill.RanksReported,
			),
		}, true
	}
	missing := make([]int, 0, len(expected))
	for _, rank := range expected {
		if _, ok := reported[rank]; ok {
			continue
		}
		missing = append(missing, rank)
	}
	if len(missing) > 0 {
		return Reason{
			Code: ReasonDrillRankCoverageIncomplete,
			Detail: fmt.Sprintf(
				"restore drill covered %d of %d ranks, missing ranks %v",
				len(expected)-len(missing),
				len(expected),
				missing,
			),
		}, true
	}
	return Reason{}, false
}

// manifestIdentityGap describes how a manifest claims to be a different
// checkpoint than the record, or returns "" when the identity agrees.
func manifestIdentityGap(
	record CheckpointRecord,
	state StateClosureSummary,
) string {
	if state.CheckpointUID != "" &&
		record.JobUID != "" &&
		state.CheckpointUID != record.JobUID {
		return fmt.Sprintf(
			"manifest describes checkpoint %q, the record is %q",
			state.CheckpointUID,
			record.JobUID,
		)
	}
	if state.CheckpointVersion != "" &&
		record.Version != "" &&
		state.CheckpointVersion != record.Version {
		return fmt.Sprintf(
			"manifest describes version %q, the record is %q",
			state.CheckpointVersion,
			record.Version,
		)
	}
	if state.CheckpointMetadataDigest != "" &&
		record.MetadataDigest != "" &&
		state.CheckpointMetadataDigest != record.MetadataDigest {
		return "manifest metadata digest does not match the checkpoint record"
	}
	return ""
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
