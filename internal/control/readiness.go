package control

import (
	"fmt"
	"time"
)

const DefaultMaxAge = 24 * time.Hour

func AssessReadiness(
	record CheckpointRecord,
	now time.Time,
	maxAge time.Duration,
	targetWorldSize *int,
	drill *DrillOutcome,
) CheckpointReadiness {
	reasons := append([]Reason(nil), record.Reasons...)
	fresh := false
	var ageSeconds *int64
	if record.LatestMtime != "" {
		latest, err := time.Parse(time.RFC3339, record.LatestMtime)
		if err == nil {
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
	}

	var movable *bool
	if targetWorldSize != nil {
		value := record.Complete && *targetWorldSize > 0
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
	if drill != nil {
		lastDrill = drill.ProofRef
		if lastDrill == "" {
			lastDrill = drill.Phase
		}
		value := drill.Succeeded
		loadable = &value
		if !value {
			detail := drill.Detail
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

	status := ReadinessReady
	switch {
	case !record.Complete:
		status = ReadinessIncomplete
	case !fresh:
		status = ReadinessStale
	case drill != nil && !drill.Succeeded:
		status = ReadinessTargetBlocked
	case loadable == nil:
		status = ReadinessUnverified
	case movable != nil && !*movable:
		status = ReadinessTargetBlocked
	}

	return CheckpointReadiness{
		Status:              status,
		Complete:            record.Complete,
		Fresh:               fresh,
		Loadable:            loadable,
		Movable:             movable,
		Reasons:             dedupeReasons(reasons),
		ObservedAt:          now.UTC().Format(time.RFC3339),
		TargetWorldSize:     targetWorldSize,
		EstimatedRTOSeconds: ageSeconds,
		LastDrill:           lastDrill,
	}
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
