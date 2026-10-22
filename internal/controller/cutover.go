package controller

import (
	"context"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

// CutoverManager is the control plane required to observe and optionally
// promote an online checkpoint version. It is deliberately separate from the
// per-rank target task commit path.
type CutoverManager interface {
	Status(context.Context) (map[string]any, error)
	Follow(context.Context) ([]map[string]any, error)
	Cutover(context.Context, string) (map[string]any, error)
	Recover(context.Context) (map[string]any, error)
}

func (r *Reconciler) reconcileCutover(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
) error {
	if r.Cutover == nil ||
		migration.Status.Phase != shardferryv1.MigrationPhaseSucceeded {
		return nil
	}
	status, err := r.Cutover.Status(ctx)
	if err != nil {
		return r.setCutoverCondition(
			ctx,
			migration,
			"failed",
			"CutoverStatusFailed",
			err.Error(),
		)
	}
	sourceLatest, _ := status["source_latest"].(string)
	state, _ := status["state"].(string)
	desiredPhase := cutoverPhaseFromService(state)
	message := "cutover service state: " + state

	if state == "cutover-incomplete" {
		result, recoverErr := r.Cutover.Recover(ctx)
		if recoverErr != nil {
			return r.setCutoverCondition(
				ctx,
				migration,
				"commit-incomplete",
				"CutoverRecoverFailed",
				recoverErr.Error(),
			)
		}
		state, _ = result["state"].(string)
		desiredPhase = cutoverPhaseFromService(state)
		message = "cutover recovered to state: " + state
	}

	if r.CutoverAutoPromote && sourceLatest != "" &&
		state != "promoted" && state != "cutover-incomplete" {
		if state == "catching-up" || state == "following" {
			if _, err := r.Cutover.Follow(ctx); err != nil {
				return r.setCutoverCondition(
					ctx,
					migration,
					"failed",
					"CutoverFollowFailed",
					err.Error(),
				)
			}
		}
		result, err := r.Cutover.Cutover(ctx, sourceLatest)
		if err != nil {
			return r.setCutoverCondition(
				ctx,
				migration,
				"failed",
				"CutoverPromoteFailed",
				err.Error(),
			)
		}
		if promoted, _ := result["state"].(string); promoted == "promoted" {
			desiredPhase = "promoted"
			message = "cutover promoted version " + sourceLatest
		}
	}
	return r.setCutoverCondition(
		ctx,
		migration,
		desiredPhase,
		"CutoverObserved",
		message,
	)
}

func cutoverPhaseFromService(state string) string {
	switch state {
	case "catching-up":
		return "catching-up"
	case "cutover-incomplete":
		return "commit-incomplete"
	case "promoted":
		return "promoted"
	case "failed":
		return "failed"
	default:
		return "following"
	}
}

func (r *Reconciler) setCutoverCondition(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	phase string,
	reason string,
	message string,
) error {
	conditionStatus := metav1.ConditionTrue
	if phase == "failed" || phase == "commit-incomplete" {
		conditionStatus = metav1.ConditionFalse
	}
	return r.updateStatus(ctx, migration, func(latest *shardferryv1.CheckpointMigration) bool {
		changed := false
		if latest.Status.CutoverPhase != phase {
			latest.Status.CutoverPhase = phase
			changed = true
		}
		if meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
			Type:               "CutoverReady",
			Status:             conditionStatus,
			Reason:             reason,
			Message:            message,
			ObservedGeneration: latest.Generation,
		}) {
			changed = true
		}
		return changed
	})
}
