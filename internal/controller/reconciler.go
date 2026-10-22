package controller

import (
	"context"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/execution"
)

type Reconciler struct {
	client.Client
	Scheme             *runtime.Scheme
	Orchestrator       *Orchestrator
	Runner             *execution.Runner
	Preparer           *PreparationRunner
	Recorder           record.EventRecorder
	Cutover            CutoverManager
	CutoverAutoPromote bool
}

func (r *Reconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var migration shardferryv1.CheckpointMigration
	if err := r.Get(ctx, request.NamespacedName, &migration); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	if err := validateMigration(&migration); err != nil {
		return ctrl.Result{}, r.setStatus(
			ctx,
			&migration,
			shardferryv1.MigrationPhaseFailed,
			metav1.ConditionFalse,
			"InvalidSpec",
			err.Error(),
		)
	}
	if r.Preparer != nil && migration.Status.ExecutionRef == "" {
		r.Preparer.Enqueue(request.NamespacedName)
		return ctrl.Result{RequeueAfter: time.Second}, nil
	}
	if r.Runner != nil &&
		migration.Status.ExecutionRef != "" &&
		migration.Status.Phase != shardferryv1.MigrationPhaseSucceeded &&
		migration.Status.Phase != shardferryv1.MigrationPhaseFailed {
		run, err := r.Runner.Load(ctx, migration.Status.ExecutionRef)
		if err != nil {
			return ctrl.Result{}, r.setStatus(
				ctx,
				&migration,
				shardferryv1.MigrationPhaseFailed,
				metav1.ConditionFalse,
				"ExecutionRestoreFailed",
				err.Error(),
			)
		}
		run = r.Runner.Ensure(run)
		if err := r.setExecutionStatus(
			ctx,
			&migration,
			run,
			migration.Status.PlanHash,
		); err != nil {
			return ctrl.Result{}, err
		}
		if run.Summary().Phase == execution.PhaseSucceeded ||
			run.Summary().Phase == execution.PhaseFailed {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{RequeueAfter: time.Second}, nil
	}
	if r.Orchestrator != nil {
		if condition := meta.FindStatusCondition(
			migration.Status.Conditions,
			"PlanReady",
		); condition != nil &&
			condition.Status == metav1.ConditionTrue &&
			condition.ObservedGeneration == migration.Generation &&
			migration.Status.Phase == shardferryv1.MigrationPhaseSucceeded {
			return ctrl.Result{}, nil
		}
		prepared, err := r.Orchestrator.Prepare(ctx, &migration)
		if err != nil {
			return ctrl.Result{}, r.setStatus(
				ctx,
				&migration,
				shardferryv1.MigrationPhaseFailed,
				metav1.ConditionFalse,
				"PlanningFailed",
				err.Error(),
			)
		}
		if r.Orchestrator.Tasks != nil && prepared.SpecRef != "" {
			result, err := r.Orchestrator.Dispatch(ctx, &migration, prepared)
			if err != nil {
				return ctrl.Result{}, r.setStatus(
					ctx,
					&migration,
					shardferryv1.MigrationPhaseFailed,
					metav1.ConditionFalse,
					"DispatchFailed",
					err.Error(),
				)
			}
			if result.Committed {
				return ctrl.Result{}, r.setDispatchSucceeded(
					ctx,
					&migration,
					prepared,
					result,
				)
			}
		}
		return ctrl.Result{}, r.setPlanReady(ctx, &migration, prepared)
	}
	return ctrl.Result{}, r.setStatus(
		ctx,
		&migration,
		shardferryv1.MigrationPhasePending,
		metav1.ConditionTrue,
		"Validated",
		"migration spec is valid; waiting for inventory and planning",
	)
}

func (r *Reconciler) setDispatchSucceeded(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	prepared Prepared,
	result DispatchResult,
) error {
	migration.Status.Phase = shardferryv1.MigrationPhaseSucceeded
	migration.Status.PlanRef = prepared.PlanRef
	migration.Status.PlanHash = prepared.PlanHash
	migration.Status.ReportRef = result.ReportRef
	migration.Status.ProofRef = result.ProofRef
	migration.Status.ProofHash = result.ProofHash
	migration.Status.CommitToken = migrationUID(migration)
	migration.Status.CutoverPhase = "promoted"
	migration.Status.Counts = shardferryv1.RankCounts{
		Committed: int32(len(result.Tasks)),
	}
	if migration.Status.StartedAt == "" {
		migration.Status.StartedAt = time.Now().UTC().Format(time.RFC3339)
	}
	migration.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionTrue,
		Reason:             "Dispatched",
		Message:            "all target ranks committed",
		ObservedGeneration: migration.Generation,
	})
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "PlanReady",
		Status:             metav1.ConditionTrue,
		Reason:             "PlanBuilt",
		Message:            "plan artifact hash " + prepared.PlanHash,
		ObservedGeneration: migration.Generation,
	})
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Dispatched",
		Status:             metav1.ConditionTrue,
		Reason:             "Committed",
		Message:            "all target ranks committed",
		ObservedGeneration: migration.Generation,
	})
	if err := r.Status().Update(ctx, migration); err != nil {
		return err
	}
	return r.reconcileCutover(ctx, migration)
}

func (r *Reconciler) setPlanReady(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	prepared Prepared,
) error {
	migration.Status.Phase = shardferryv1.MigrationPhasePending
	migration.Status.PlanHash = prepared.PlanHash
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionTrue,
		Reason:             "Validated",
		Message:            "migration spec is valid",
		ObservedGeneration: migration.Generation,
	})
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "PlanReady",
		Status:             metav1.ConditionTrue,
		Reason:             "PlanBuilt",
		Message:            "plan artifact hash " + prepared.PlanHash,
		ObservedGeneration: migration.Generation,
	})
	return r.Status().Update(ctx, migration)
}

func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&shardferryv1.CheckpointMigration{}).
		Complete(r)
}

func (r *Reconciler) setStatus(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	phase string,
	status metav1.ConditionStatus,
	reason string,
	message string,
) error {
	changed := migration.Status.Phase != phase
	if meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: migration.Generation,
	}) {
		changed = true
	}
	if !changed {
		return nil
	}
	migration.Status.Phase = phase
	return r.Status().Update(ctx, migration)
}

func (r *Reconciler) updateStatus(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	mutate func(*shardferryv1.CheckpointMigration) bool,
) error {
	key := types.NamespacedName{
		Namespace: migration.Namespace,
		Name:      migration.Name,
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest shardferryv1.CheckpointMigration
		if err := r.Get(ctx, key, &latest); err != nil {
			return err
		}
		if !mutate(&latest) {
			return nil
		}
		return r.Status().Update(ctx, &latest)
	})
}

func (r *Reconciler) setExecutionStatus(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	run *execution.Run,
	planHash string,
) error {
	summary := run.Summary()
	phase := executionPhase(summary.Phase)
	err := r.updateStatus(ctx, migration, func(latest *shardferryv1.CheckpointMigration) bool {
		changed := latest.Status.Phase != phase ||
			latest.Status.PlanRef != run.PlanRef() ||
			latest.Status.PlanHash != planHash ||
			latest.Status.ExecutionRef != run.ExecutionRef() ||
			latest.Status.ReportRef != run.ReportRef() ||
			latest.Status.ProofRef != run.ProofRef() ||
			latest.Status.ProofHash != run.ProofHash()
		latest.Status.Phase = phase
		latest.Status.PlanRef = run.PlanRef()
		latest.Status.PlanHash = planHash
		latest.Status.ExecutionRef = run.ExecutionRef()
		latest.Status.ReportRef = run.ReportRef()
		latest.Status.ProofRef = run.ProofRef()
		latest.Status.ProofHash = run.ProofHash()
		latest.Status.Counts = shardferryv1.RankCounts{
			Prepared:  int32(summary.Prepared),
			Staged:    int32(summary.Staged),
			Committed: int32(summary.Committed),
			Failed:    int32(summary.Failed),
		}
		if phase != shardferryv1.MigrationPhasePending &&
			latest.Status.StartedAt == "" {
			latest.Status.StartedAt = time.Now().UTC().Format(time.RFC3339)
			changed = true
		}
		if phase == shardferryv1.MigrationPhaseSucceeded ||
			phase == shardferryv1.MigrationPhaseFailed ||
			phase == shardferryv1.MigrationPhaseCancelled {
			if latest.Status.CompletedAt == "" {
				latest.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
				changed = true
			}
		}
		if latest.Status.CutoverPhase == "" {
			latest.Status.CutoverPhase = cutoverPhase(summary.Phase)
			changed = true
		}
		if meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
			Type:               "Accepted",
			Status:             metav1.ConditionTrue,
			Reason:             "ExecutionInProgress",
			Message:            "target rank work is in progress",
			ObservedGeneration: latest.Generation,
		}) {
			changed = true
		}
		if phase == shardferryv1.MigrationPhaseSucceeded ||
			phase == shardferryv1.MigrationPhaseFailed ||
			phase == shardferryv1.MigrationPhaseCancelled {
			if meta.SetStatusCondition(&latest.Status.Conditions, metav1.Condition{
				Type:               "Executing",
				Status:             metav1.ConditionFalse,
				Reason:             "ExecutionFinished",
				Message:            "execution finished",
				ObservedGeneration: latest.Generation,
			}) {
				changed = true
			}
		}
		return changed
	})
	if err == nil && summary.Phase == execution.PhaseSucceeded {
		return r.reconcileCutover(ctx, migration)
	}
	return err
}

func executionPhase(phase execution.Phase) string {
	switch phase {
	case execution.PhaseSucceeded:
		return shardferryv1.MigrationPhaseSucceeded
	case execution.PhaseFailed:
		return shardferryv1.MigrationPhaseFailed
	case execution.PhaseCancelled:
		return shardferryv1.MigrationPhaseCancelled
	case execution.PhaseCommitting:
		return shardferryv1.MigrationPhaseCommitting
	case execution.PhaseCommitIncomplete:
		return shardferryv1.MigrationPhaseCommitIncomplete
	case execution.PhaseStaged:
		return shardferryv1.MigrationPhaseTransferring
	case execution.PhaseFinalizing:
		return shardferryv1.MigrationPhaseCommitPending
	case execution.PhasePrepared:
		return shardferryv1.MigrationPhasePreparing
	default:
		return shardferryv1.MigrationPhasePending
	}
}

func cutoverPhase(phase execution.Phase) string {
	switch phase {
	case execution.PhasePrepared:
		return "catching-up"
	case execution.PhaseStaged:
		return "target-verified"
	case execution.PhaseFinalizing:
		return "promoting"
	case execution.PhaseCommitting:
		return "promoting"
	case execution.PhaseCommitted, execution.PhaseSucceeded:
		return "promoted"
	case execution.PhaseCommitIncomplete:
		return "commit-incomplete"
	case execution.PhaseFailed:
		return "failed"
	case execution.PhaseCancelled:
		return "cancelled"
	default:
		return "following"
	}
}

func validateMigration(migration *shardferryv1.CheckpointMigration) error {
	spec := &migration.Spec
	if spec.Checkpoint.Format != "dcp" {
		return fmt.Errorf("unsupported checkpoint format: %s", spec.Checkpoint.Format)
	}
	if spec.Checkpoint.FormatVersion == "" {
		return fmt.Errorf("checkpoint formatVersion is required")
	}
	if err := validateMesh("source", spec.Source.WorldSize, spec.Source.Mesh); err != nil {
		return err
	}
	if err := validateMesh("target", spec.Target.WorldSize, spec.Target.Mesh); err != nil {
		return err
	}
	if err := validateAgentOrRanks(
		"source",
		spec.Source.WorldSize,
		spec.Source.RankMap,
		spec.Source.AgentSelector,
		spec.Source.ClusterRef,
	); err != nil {
		return err
	}
	if err := validateAgentOrRanks(
		"target",
		spec.Target.WorldSize,
		spec.Target.RankMap,
		spec.Target.AgentSelector,
		spec.Target.ClusterRef,
	); err != nil {
		return err
	}
	if spec.Source.MetadataOwnerRank < 0 ||
		spec.Source.MetadataOwnerRank >= spec.Source.WorldSize {
		return fmt.Errorf("source.metadataOwnerRank is out of range")
	}
	if spec.Source.CheckpointRoot == "" {
		return fmt.Errorf("source.checkpointRoot is required")
	}
	if spec.Target.RootTemplate == "" {
		return fmt.Errorf("target.rootTemplate is required")
	}
	if spec.Policy.Verify != "bitwise" {
		return fmt.Errorf("only bitwise verification is supported")
	}
	if spec.Policy.Commit != "all-or-nothing" {
		return fmt.Errorf("only all-or-nothing commit is supported")
	}
	if spec.Policy.MaxConcurrentTransfers < 1 ||
		spec.Policy.MaxAttemptsPerRank < 1 ||
		spec.Policy.TimeoutSeconds < 1 ||
		spec.Policy.SourceChunkCacheBytes < 1 {
		return fmt.Errorf("policy limits must be positive")
	}
	switch spec.Artifacts.Store {
	case "pvc":
		if spec.Artifacts.ClaimName == "" {
			return fmt.Errorf("artifacts.claimName is required for pvc")
		}
	case "s3":
		if spec.Artifacts.Bucket == "" {
			return fmt.Errorf("artifacts.bucket is required for s3")
		}
	default:
		return fmt.Errorf("unsupported artifact store: %s", spec.Artifacts.Store)
	}
	return nil
}

// ValidateMigration exposes the same API validation used by the controller.
func ValidateMigration(migration *shardferryv1.CheckpointMigration) error {
	return validateMigration(migration)
}

func validateMesh(role string, worldSize int32, mesh shardferryv1.MeshSpec) error {
	if worldSize < 1 || worldSize > 256 {
		return fmt.Errorf("%s.worldSize is out of range", role)
	}
	if len(mesh.Shape) == 0 || len(mesh.Shape) != len(mesh.Placements) {
		return fmt.Errorf("%s.mesh shape/placements mismatch", role)
	}
	var product int32 = 1
	for _, dimension := range mesh.Shape {
		if dimension < 1 {
			return fmt.Errorf("%s.mesh shape contains a non-positive dimension", role)
		}
		product *= dimension
	}
	if product != worldSize {
		return fmt.Errorf("%s.mesh product %d != worldSize %d", role, product, worldSize)
	}
	return nil
}

func validateRanks(role string, worldSize int32, ranks []shardferryv1.RankEndpoint) error {
	if len(ranks) != int(worldSize) {
		return fmt.Errorf(
			"%s.rankMap has %d entries, want %d",
			role,
			len(ranks),
			worldSize,
		)
	}
	seen := make(map[int32]struct{}, len(ranks))
	for _, entry := range ranks {
		if entry.Rank < 0 || entry.Rank >= worldSize {
			return fmt.Errorf("%s.rankMap rank %d is out of range", role, entry.Rank)
		}
		if entry.Endpoint == "" {
			return fmt.Errorf("%s.rankMap rank %d has an empty endpoint", role, entry.Rank)
		}
		if _, exists := seen[entry.Rank]; exists {
			return fmt.Errorf("%s.rankMap contains duplicate rank %d", role, entry.Rank)
		}
		seen[entry.Rank] = struct{}{}
	}
	return nil
}

func validateAgentOrRanks(
	role string,
	worldSize int32,
	ranks []shardferryv1.RankEndpoint,
	selector *shardferryv1.AgentSelector,
	clusterRef string,
) error {
	if clusterRef != "" {
		if len(ranks) > 0 {
			return fmt.Errorf(
				"%s must not set rankMap when clusterRef is set",
				role,
			)
		}
		if selector == nil || len(selector.MatchLabels) == 0 {
			return fmt.Errorf(
				"%s.agentSelector.matchLabels is required with clusterRef",
				role,
			)
		}
		if selector.Port < 0 || selector.Port > 65535 {
			return fmt.Errorf("%s.agentSelector.port is out of range", role)
		}
		switch selector.Scheme {
		case "", "http", "https":
		default:
			return fmt.Errorf("%s.agentSelector.scheme must be http or https", role)
		}
		return nil
	}
	if len(ranks) > 0 {
		if selector != nil {
			return fmt.Errorf(
				"%s must set either rankMap or agentSelector, not both",
				role,
			)
		}
		return validateRanks(role, worldSize, ranks)
	}
	if selector == nil {
		return fmt.Errorf(
			"%s.rankMap or %s.agentSelector is required",
			role,
			role,
		)
	}
	if len(selector.MatchLabels) == 0 {
		return fmt.Errorf("%s.agentSelector.matchLabels is required", role)
	}
	if selector.Port < 0 || selector.Port > 65535 {
		return fmt.Errorf("%s.agentSelector.port is out of range", role)
	}
	switch selector.Scheme {
	case "", "http", "https":
	default:
		return fmt.Errorf("%s.agentSelector.scheme must be http or https", role)
	}
	return nil
}
