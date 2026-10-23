package main

import (
	"fmt"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

// nextAction is intentionally user-facing: every terminal or waiting state
// should leave the operator with one concrete next command or check.
func nextAction(migration *aevrelv1.CheckpointMigration) string {
	name := fmt.Sprintf("%s/%s", migration.Namespace, migration.Name)
	phase := migration.Status.Phase

	if reason := conditionReason(migration, "LiveReady"); reason == "LiveExecutorUnavailable" {
		return fmt.Sprintf(
			"configure controller.liveURL (or --live-url) and retry %s; live mode never falls back to static implicitly",
			name,
		)
	}
	if reason := conditionReason(migration, "TargetReady"); reason != "" &&
		phase == aevrelv1.MigrationPhaseTargetFailed {
		return fmt.Sprintf(
			"inspect `aevrelctl describe migration %s --ranks`, fix the target Job/state, then `aevrelctl retry migration %s`",
			name,
			name,
		)
	}

	switch phase {
	case aevrelv1.MigrationPhaseSucceeded:
		return fmt.Sprintf(
			"verify the target with `aevrel-target-load` or inspect proof %s",
			emptyDash(migration.Status.ProofRef),
		)
	case aevrelv1.MigrationPhaseTargetValidating:
		return fmt.Sprintf(
			"wait for the target-ready Job; inspect `aevrelctl describe migration %s --ranks`",
			name,
		)
	case aevrelv1.MigrationPhaseTargetFailed:
		return fmt.Sprintf(
			"inspect `aevrelctl describe migration %s --ranks`; fix the target and retry",
			name,
		)
	case aevrelv1.MigrationPhaseCommitIncomplete:
		return fmt.Sprintf(
			"run `aevrelctl retry migration %s`; repair the commit, do not re-transfer committed ranks",
			name,
		)
	case aevrelv1.MigrationPhaseFailed:
		return fmt.Sprintf(
			"inspect conditions and `aevrelctl describe migration %s --ranks`; fix the cause, then retry",
			name,
		)
	case aevrelv1.MigrationPhaseCancelled:
		return fmt.Sprintf(
			"inspect whether target ranks were committed; retry %s only after confirming the old target is safe",
			name,
		)
	}

	if migration.Spec.Mode == aevrelv1.MigrationModeLiveRankSplit {
		if live := migration.Status.Live; live != nil && live.CutoverPhase == "cutover" {
			return fmt.Sprintf(
				"cutover is in progress; do not start a second migration; watch `aevrelctl describe migration %s --ranks`",
				name,
			)
		}
		return fmt.Sprintf(
			"live mode is following; watch `aevrelctl describe migration %s --ranks` for coverage/quorum/target-ready",
			name,
		)
	}

	return fmt.Sprintf(
		"wait for the controller or rerun `aevrelctl migrate -f <file> --wait`; inspect `aevrelctl describe migration %s --ranks`",
		name,
	)
}

func conditionReason(
	migration *aevrelv1.CheckpointMigration,
	conditionType string,
) string {
	for _, condition := range migration.Status.Conditions {
		if condition.Type == conditionType {
			return condition.Reason
		}
	}
	return ""
}
