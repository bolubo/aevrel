package controller

import (
	"context"
	"fmt"
	"reflect"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

const liveRankSplitPollInterval = 2 * time.Second

// LiveRankSplitObservation is the data-plane state reported by the LRS
// executor. The controller remains the source of truth for CR status and
// promotion gates.
type LiveRankSplitObservation struct {
	Phase         string                    `json:"phase"`
	InFlight      []int32                   `json:"inFlight,omitempty"`
	Coverage      []int32                   `json:"coverage,omitempty"`
	Quorum        int32                     `json:"quorum,omitempty"`
	StateComplete bool                      `json:"stateComplete,omitempty"`
	StateRoot     string                    `json:"stateRoot,omitempty"`
	ProofRef      string                    `json:"proofRef,omitempty"`
	Message       string                    `json:"message,omitempty"`
	Ranks         []aevrelv1.LiveRankStatus `json:"ranks,omitempty"`
}

// LiveRankSplitExecutor owns the live data-plane handoff. The controller
// owns validation, fencing, status, and fail-closed semantics; the executor
// owns rank snapshots/deltas and target join.
type LiveRankSplitExecutor interface {
	Observe(
		context.Context,
		*aevrelv1.CheckpointMigration,
	) (LiveRankSplitObservation, error)
}

func (r *Reconciler) reconcileLiveRankSplit(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (ctrl.Result, error) {
	switch migration.Status.Phase {
	case aevrelv1.MigrationPhaseSucceeded,
		aevrelv1.MigrationPhaseFailed,
		aevrelv1.MigrationPhaseCancelled:
		return ctrl.Result{}, nil
	}
	if r.Live == nil {
		return r.failLiveRankSplit(
			ctx,
			migration,
			"LiveExecutorUnavailable",
			"live-rank-split executor is not configured",
		)
	}
	observation, err := r.Live.Observe(ctx, migration)
	if err != nil {
		message := "observe live-rank-split executor: " + err.Error()
		requeue := liveRankSplitPollInterval
		return ctrl.Result{RequeueAfter: requeue}, r.updateStatus(
			ctx,
			migration,
			func(latest *aevrelv1.CheckpointMigration) bool {
				before := latest.Status.DeepCopy()
				latest.Status.Phase = aevrelv1.MigrationPhaseTransferring
				latest.Status.Live = &aevrelv1.LiveRankSplitStatus{
					CutoverPhase: "failed",
					Message:      message,
				}
				meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
					Type:               "LiveReady",
					Status:             metav1.ConditionFalse,
					Reason:             "LiveExecutorUnavailable",
					Message:            message,
					ObservedGeneration: latest.Generation,
				})
				return !reflect.DeepEqual(before, &latest.Status)
			},
		)
	}
	if err := validateLiveRankSplitObservation(migration, observation); err != nil {
		return r.failLiveRankSplit(
			ctx,
			migration,
			"LiveObservationInvalid",
			err.Error(),
		)
	}
	return r.persistLiveRankSplitObservation(ctx, migration, observation)
}

func (r *Reconciler) failLiveRankSplit(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	reason string,
	message string,
) (ctrl.Result, error) {
	return ctrl.Result{}, r.updateStatus(
		ctx,
		migration,
		func(latest *aevrelv1.CheckpointMigration) bool {
			before := latest.Status.DeepCopy()
			latest.Status.Phase = aevrelv1.MigrationPhaseFailed
			latest.Status.Live = &aevrelv1.LiveRankSplitStatus{
				CutoverPhase: "failed",
				Message:      message,
			}
			latest.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
			meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
				Type:               "LiveReady",
				Status:             metav1.ConditionFalse,
				Reason:             reason,
				Message:            message,
				ObservedGeneration: latest.Generation,
			})
			return !reflect.DeepEqual(before, &latest.Status)
		},
	)
}

func (r *Reconciler) persistLiveRankSplitObservation(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	observation LiveRankSplitObservation,
) (ctrl.Result, error) {
	phase := liveMigrationPhase(observation.Phase)
	livePhase := observation.Phase
	message := observation.Message
	conditionStatus := metav1.ConditionUnknown
	conditionReason := "LiveInProgress"
	if phase == aevrelv1.MigrationPhaseFailed {
		conditionStatus = metav1.ConditionFalse
		conditionReason = "LiveFailed"
	}
	if phase == aevrelv1.MigrationPhaseCancelled {
		conditionStatus = metav1.ConditionFalse
		conditionReason = "LiveCancelled"
	}
	if observation.Phase == "succeeded" &&
		migration.Spec.Target.Validation != nil &&
		(migration.Status.TargetReady == nil || !migration.Status.TargetReady.Ready) {
		phase = aevrelv1.MigrationPhaseTargetValidating
		livePhase = "target-validating"
		conditionReason = "TargetValidationRequired"
		if message == "" {
			message = "live rank split completed; target validation is required"
		}
	}
	if observation.Phase == "succeeded" &&
		phase == aevrelv1.MigrationPhaseSucceeded {
		conditionStatus = metav1.ConditionTrue
		conditionReason = "LiveCompleted"
	}
	if phase == aevrelv1.MigrationPhaseTargetValidating {
		conditionStatus = metav1.ConditionUnknown
		conditionReason = "TargetValidationRequired"
	}
	quorum := observation.Quorum
	if quorum == 0 {
		quorum = liveQuorum(migration)
	}
	stateRoot := observation.StateRoot
	if stateRoot == "" {
		stateRoot = migration.Spec.Live.StateRoot
	}
	err := r.updateStatus(
		ctx,
		migration,
		func(latest *aevrelv1.CheckpointMigration) bool {
			before := latest.Status.DeepCopy()
			latest.Status.Phase = phase
			latest.Status.Live = &aevrelv1.LiveRankSplitStatus{
				CutoverPhase:  livePhase,
				InFlight:      append([]int32(nil), observation.InFlight...),
				Coverage:      append([]int32(nil), observation.Coverage...),
				Quorum:        quorum,
				StateComplete: observation.StateComplete,
				StateRoot:     stateRoot,
				ProofRef:      observation.ProofRef,
				Message:       message,
				Ranks:         append([]aevrelv1.LiveRankStatus(nil), observation.Ranks...),
			}
			if observation.ProofRef != "" {
				latest.Status.ProofRef = observation.ProofRef
			}
			switch phase {
			case aevrelv1.MigrationPhaseSucceeded,
				aevrelv1.MigrationPhaseFailed,
				aevrelv1.MigrationPhaseCancelled:
				if latest.Status.CompletedAt == "" {
					latest.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
				}
				if latest.Status.Live.CompletedAt == "" {
					latest.Status.Live.CompletedAt = latest.Status.CompletedAt
				}
			}
			meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
				Type:               "LiveReady",
				Status:             conditionStatus,
				Reason:             conditionReason,
				Message:            message,
				ObservedGeneration: latest.Generation,
			})
			return !reflect.DeepEqual(before, &latest.Status)
		},
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	switch phase {
	case aevrelv1.MigrationPhaseSucceeded,
		aevrelv1.MigrationPhaseFailed,
		aevrelv1.MigrationPhaseCancelled:
		return ctrl.Result{}, nil
	case aevrelv1.MigrationPhaseTargetValidating:
		return ctrl.Result{RequeueAfter: time.Second}, nil
	default:
		return ctrl.Result{RequeueAfter: liveRankSplitPollInterval}, nil
	}
}

func liveMigrationPhase(livePhase string) string {
	switch livePhase {
	case "pending", "preflight":
		return aevrelv1.MigrationPhasePreparing
	case "draining", "snapshotting", "following", "cutover":
		return aevrelv1.MigrationPhaseTransferring
	case "target-validating":
		return aevrelv1.MigrationPhaseTargetValidating
	case "succeeded":
		return aevrelv1.MigrationPhaseSucceeded
	case "failed":
		return aevrelv1.MigrationPhaseFailed
	case "cancelled":
		return aevrelv1.MigrationPhaseCancelled
	default:
		return aevrelv1.MigrationPhaseFailed
	}
}

func validateLiveRankSplitObservation(
	migration *aevrelv1.CheckpointMigration,
	observation LiveRankSplitObservation,
) error {
	switch observation.Phase {
	case "pending", "preflight", "draining", "snapshotting", "following",
		"cutover", "target-validating", "succeeded", "failed", "cancelled":
	default:
		return fmt.Errorf("unknown live phase %q", observation.Phase)
	}
	worldSize := migration.Spec.Source.WorldSize
	if err := validateLiveRanks("inFlight", observation.InFlight, worldSize); err != nil {
		return err
	}
	if err := validateLiveRanks("coverage", observation.Coverage, worldSize); err != nil {
		return err
	}
	ranks := make([]int32, 0, len(observation.Ranks))
	for _, rank := range observation.Ranks {
		ranks = append(ranks, rank.Rank)
	}
	if err := validateLiveRanks("ranks", ranks, worldSize); err != nil {
		return err
	}
	if observation.Quorum < 0 || observation.Quorum > worldSize {
		return fmt.Errorf("live quorum is out of range")
	}
	if observation.Phase != "succeeded" &&
		observation.Phase != "target-validating" {
		return nil
	}
	if len(observation.InFlight) > 0 {
		return fmt.Errorf("completed live split still has in-flight ranks")
	}
	if len(observation.Coverage) != int(worldSize) {
		return fmt.Errorf(
			"completed live split covers %d ranks, want %d",
			len(observation.Coverage),
			worldSize,
		)
	}
	requiredQuorum := liveQuorum(migration)
	if observation.Quorum < requiredQuorum {
		return fmt.Errorf(
			"live quorum %d is below required %d",
			observation.Quorum,
			requiredQuorum,
		)
	}
	if liveRequiresState(migration) && !observation.StateComplete {
		return fmt.Errorf("L4 live split completed without state closure")
	}
	if observation.Phase == "target-validating" &&
		migration.Spec.Target.Validation == nil {
		return fmt.Errorf("target validation is not configured")
	}
	return nil
}

func validateLiveRanks(name string, ranks []int32, worldSize int32) error {
	seen := make(map[int32]struct{}, len(ranks))
	for _, rank := range ranks {
		if rank < 0 || rank >= worldSize {
			return fmt.Errorf("%s rank %d is out of range", name, rank)
		}
		if _, ok := seen[rank]; ok {
			return fmt.Errorf("%s rank %d is duplicated", name, rank)
		}
		seen[rank] = struct{}{}
	}
	return nil
}

func liveQuorum(migration *aevrelv1.CheckpointMigration) int32 {
	if migration.Spec.Live == nil {
		return 0
	}
	if migration.Spec.Live.Cutover.Quorum > 0 {
		return migration.Spec.Live.Cutover.Quorum
	}
	return migration.Spec.Source.WorldSize/2 + 1
}

func liveRequiresState(
	migration *aevrelv1.CheckpointMigration,
) bool {
	if migration.Spec.Mode != aevrelv1.MigrationModeLiveRankSplit ||
		migration.Spec.Live == nil {
		return false
	}
	mode := migration.Spec.Live.StateMode
	if mode == "" {
		mode = aevrelv1.LiveStateModeL4
	}
	requireState := true
	if migration.Spec.Live.Cutover.RequireStateComplete != nil {
		requireState = *migration.Spec.Live.Cutover.RequireStateComplete
	}
	return mode == aevrelv1.LiveStateModeL4 || requireState
}

func validateMigrationMode(
	spec *aevrelv1.CheckpointMigrationSpec,
) error {
	mode := spec.Mode
	if mode == "" {
		mode = aevrelv1.MigrationModeStatic
	}
	switch mode {
	case aevrelv1.MigrationModeStatic:
		if spec.Live != nil {
			return fmt.Errorf(
				"live is only valid when mode=%s",
				aevrelv1.MigrationModeLiveRankSplit,
			)
		}
		return nil
	case aevrelv1.MigrationModeLiveRankSplit:
	default:
		return fmt.Errorf("unsupported migration mode: %s", mode)
	}
	live := spec.Live
	if live == nil {
		return fmt.Errorf(
			"live is required when mode=%s",
			aevrelv1.MigrationModeLiveRankSplit,
		)
	}
	if spec.Source.WorldSize < 2 {
		return fmt.Errorf(
			"live-rank-split requires source.worldSize >= 2",
		)
	}
	if spec.Source.WorldSize != spec.Target.WorldSize {
		return fmt.Errorf(
			"live-rank-split requires source.worldSize == target.worldSize",
		)
	}
	if live.MaxInFlight < 1 || live.MaxInFlight >= spec.Source.WorldSize {
		return fmt.Errorf(
			"live.maxInFlight must be in [1, source.worldSize)",
		)
	}
	stateMode := live.StateMode
	if stateMode == "" {
		stateMode = aevrelv1.LiveStateModeL4
	}
	if stateMode != aevrelv1.LiveStateModeL3 &&
		stateMode != aevrelv1.LiveStateModeL4 {
		return fmt.Errorf("live.stateMode must be L3 or L4")
	}
	if live.StateRoot == "" {
		return fmt.Errorf("live.stateRoot is required")
	}
	if len(live.StateProviders) == 0 {
		return fmt.Errorf("live.stateProviders must not be empty")
	}
	if live.Cutover.Quorum < 0 ||
		live.Cutover.Quorum > spec.Source.WorldSize {
		return fmt.Errorf("live.cutover.quorum is out of range")
	}
	if live.Cutover.Quorum > 0 &&
		live.Cutover.Quorum < spec.Source.WorldSize/2+1 {
		return fmt.Errorf(
			"live.cutover.quorum must be at least %d",
			spec.Source.WorldSize/2+1,
		)
	}
	if stateMode == aevrelv1.LiveStateModeL4 &&
		spec.Target.Validation == nil {
		return fmt.Errorf(
			"live.stateMode=L4 requires target.validation",
		)
	}
	if live.RankPlan != nil {
		if live.RankPlan.TP < 0 || live.RankPlan.PP < 0 {
			return fmt.Errorf(
				"live.rankPlan tp/pp must be non-negative",
			)
		}
		seen := map[int32]bool{}
		for _, rank := range live.RankPlan.Ranks {
			if rank < 0 || rank >= spec.Source.WorldSize {
				return fmt.Errorf(
					"live.rankPlan rank %d is out of range",
					rank,
				)
			}
			if seen[rank] {
				return fmt.Errorf(
					"live.rankPlan rank %d is duplicated",
					rank,
				)
			}
			seen[rank] = true
		}
	}
	return nil
}
