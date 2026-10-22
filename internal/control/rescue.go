package control

import (
	"fmt"
	"time"
)

const (
	RescueReasonRestoreReady         = "RESTORE_READY"
	RescueReasonMigrateReady         = "MIGRATE_READY"
	RescueReasonRestartFallback      = "RESTART_FALLBACK"
	RescueReasonCheckpointMissing    = "CHECKPOINT_NOT_FOUND"
	RescueReasonCheckpointIncomplete = "CHECKPOINT_INCOMPLETE"
	RescueReasonCheckpointStale      = "CHECKPOINT_STALE"
	RescueReasonDrillMissing         = "DRILL_MISSING"
	RescueReasonDrillFailed          = "DRILL_FAILED"
	RescueReasonProofMissing         = "PROOF_MISSING"
	RescueReasonTargetUnknown        = "TARGET_UNKNOWN"
	RescueReasonTargetBlocked        = "TARGET_BLOCKED"
)

// DecideRescue turns a checkpoint's verified readiness into an advisory action.
// It never starts a workload itself and never treats metadata as load proof.
func DecideRescue(
	event string,
	target string,
	fallback string,
	readiness *CheckpointReadiness,
	now time.Time,
) RescueDecision {
	decision := RescueDecision{
		Event:     event,
		Target:    target,
		DecidedAt: now.UTC().Format(time.RFC3339),
	}
	hold := func(code, reason string) RescueDecision {
		decision.Action = RescueHold
		decision.Code = code
		decision.Reason = reason
		return decision
	}
	fallbackOrHold := func(code, reason string) RescueDecision {
		if fallback == "restart" {
			decision.Action = RescueRestart
			decision.Code = RescueReasonRestartFallback
			decision.Reason = "explicit restart fallback: " + reason
			return decision
		}
		return hold(code, reason)
	}

	if readiness == nil {
		return fallbackOrHold(
			RescueReasonCheckpointMissing,
			"no CheckpointRecord is associated with this workload",
		)
	}
	decision.ProofRef = readiness.LastDrill
	if !readiness.Complete {
		return fallbackOrHold(
			RescueReasonCheckpointIncomplete,
			"checkpoint inventory is incomplete",
		)
	}
	if !readiness.Fresh {
		return fallbackOrHold(
			RescueReasonCheckpointStale,
			"checkpoint is older than the freshness window",
		)
	}
	if readiness.Loadable == nil {
		return fallbackOrHold(
			RescueReasonDrillMissing,
			"no restore drill has verified that the checkpoint is loadable",
		)
	}
	if !*readiness.Loadable {
		return fallbackOrHold(
			RescueReasonDrillFailed,
			"the latest restore drill did not prove loadability",
		)
	}
	if readiness.LastDrill == "" {
		return hold(
			RescueReasonProofMissing,
			"loadable is set without a restore drill reference",
		)
	}
	if target != "" {
		if readiness.Movable == nil {
			return hold(
				RescueReasonTargetUnknown,
				"checkpoint mobility to the requested target is unknown",
			)
		}
		if !*readiness.Movable {
			return hold(
				RescueReasonTargetBlocked,
				"checkpoint cannot be moved to the requested target",
			)
		}
		decision.Action = RescueMigrate
		decision.Code = RescueReasonMigrateReady
		decision.Reason = fmt.Sprintf(
			"fresh checkpoint passed a restore drill and is movable to %s",
			target,
		)
		return decision
	}
	decision.Action = RescueRestore
	decision.Code = RescueReasonRestoreReady
	decision.Reason = "fresh checkpoint passed a restore drill and can be restored"
	return decision
}
