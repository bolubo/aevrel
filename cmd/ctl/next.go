package main

import (
	"fmt"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

// nextAction is intentionally user-facing: every terminal or waiting state
// should leave the operator with one concrete next command or check.
func nextAction(migration *shardferryv1.CheckpointMigration) string {
	name := fmt.Sprintf("%s/%s", migration.Namespace, migration.Name)
	phase := migration.Status.Phase

	if reason := conditionReason(migration, "LiveReady"); reason == "LiveExecutorUnavailable" {
		return fmt.Sprintf(
			"configure controller.liveURL (or --live-url) and retry %s; live mode never falls back to static implicitly",
			name,
		)
	}
	if reason := conditionReason(migration, "TargetReady"); reason != "" &&
		phase == shardferryv1.MigrationPhaseTargetFailed {
		return fmt.Sprintf(
			"inspect `shardferryctl describe migration %s --ranks`, fix the target Job/state, then `shardferryctl retry migration %s`",
			name,
			name,
		)
	}

	switch phase {
	case shardferryv1.MigrationPhaseSucceeded:
		return fmt.Sprintf(
			"verify the target with `shardferry-target-load` or inspect proof %s",
			emptyDash(migration.Status.ProofRef),
		)
	case shardferryv1.MigrationPhaseTargetValidating:
		return fmt.Sprintf(
			"wait for the target-ready Job; inspect `shardferryctl describe migration %s --ranks`",
			name,
		)
	case shardferryv1.MigrationPhaseTargetFailed:
		return fmt.Sprintf(
			"inspect `shardferryctl describe migration %s --ranks`; fix the target and retry",
			name,
		)
	case shardferryv1.MigrationPhaseCommitIncomplete:
		return fmt.Sprintf(
			"run `shardferryctl retry migration %s`; repair the commit, do not re-transfer committed ranks",
			name,
		)
	case shardferryv1.MigrationPhaseFailed:
		return fmt.Sprintf(
			"inspect conditions and `shardferryctl describe migration %s --ranks`; fix the cause, then retry",
			name,
		)
	case shardferryv1.MigrationPhaseCancelled:
		return fmt.Sprintf(
			"inspect whether target ranks were committed; retry %s only after confirming the old target is safe",
			name,
		)
	}

	if migration.Spec.Mode == shardferryv1.MigrationModeLiveRankSplit {
		if live := migration.Status.Live; live != nil && live.CutoverPhase == "cutover" {
			return fmt.Sprintf(
				"cutover is in progress; do not start a second migration; watch `shardferryctl describe migration %s --ranks`",
				name,
			)
		}
		return fmt.Sprintf(
			"live mode is following; watch `shardferryctl describe migration %s --ranks` for coverage/quorum/target-ready",
			name,
		)
	}

	return fmt.Sprintf(
		"wait for the controller or rerun `shardferryctl migrate -f <file> --wait`; inspect `shardferryctl describe migration %s --ranks`",
		name,
	)
}

func conditionReason(
	migration *shardferryv1.CheckpointMigration,
	conditionType string,
) string {
	for _, condition := range migration.Status.Conditions {
		if condition.Type == conditionType {
			return condition.Reason
		}
	}
	return ""
}
