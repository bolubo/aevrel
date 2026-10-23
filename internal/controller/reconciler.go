package controller

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/artifactstore"
	"github.com/bolubo/aevrel/internal/execution"
)

type Reconciler struct {
	client.Client
	Scheme       *runtime.Scheme
	Orchestrator *Orchestrator
	Runner       *execution.Runner
	Preparer     *PreparationRunner
	Resolver     *AgentResolver
	Cleaner      TargetCleaner
	Artifacts    artifactstore.Store
	Recorder     record.EventRecorder
	Live         LiveRankSplitExecutor
}

type TargetCleaner interface {
	CleanupTarget(context.Context, string, string, bool) (map[string]any, error)
}

func (r *Reconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var migration aevrelv1.CheckpointMigration
	if err := r.Get(ctx, request.NamespacedName, &migration); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	if migration.DeletionTimestamp != nil {
		return r.handleDeletion(ctx, &migration)
	}

	if handled, err := r.handleControlAnnotations(ctx, &migration); handled {
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	if err := validateMigration(&migration); err != nil {
		return ctrl.Result{}, r.setStatus(
			ctx,
			&migration,
			aevrelv1.MigrationPhaseFailed,
			metav1.ConditionFalse,
			"InvalidSpec",
			err.Error(),
		)
	}
	if migration.Spec.Mode == aevrelv1.MigrationModeLiveRankSplit {
		if migration.Status.Phase == aevrelv1.MigrationPhaseTargetValidating ||
			migration.Status.Phase == aevrelv1.MigrationPhaseTargetFailed {
			return r.reconcileTargetReady(ctx, &migration)
		}
		return r.reconcileLiveRankSplit(ctx, &migration)
	}
	if migration.Status.Phase == aevrelv1.MigrationPhaseTargetValidating {
		return r.reconcileTargetReady(ctx, &migration)
	}
	if migration.Status.Phase == aevrelv1.MigrationPhaseTargetFailed {
		return ctrl.Result{}, nil
	}
	if migration.Status.Phase == aevrelv1.MigrationPhaseSucceeded {
		return ctrl.Result{}, nil
	}
	if r.Preparer != nil && migration.Status.ExecutionRef == "" {
		r.Preparer.Enqueue(request.NamespacedName)
		return ctrl.Result{RequeueAfter: time.Second}, nil
	}
	if r.Runner != nil &&
		migration.Status.ExecutionRef != "" &&
		migration.Status.Phase != aevrelv1.MigrationPhaseSucceeded &&
		migration.Status.Phase != aevrelv1.MigrationPhaseFailed &&
		migration.Status.Phase != aevrelv1.MigrationPhaseTargetValidating &&
		migration.Status.Phase != aevrelv1.MigrationPhaseTargetFailed {
		if r.Resolver != nil {
			if err := r.Resolver.EnsureTokens(ctx, &migration); err != nil {
				return ctrl.Result{}, r.setStatus(
					ctx,
					&migration,
					aevrelv1.MigrationPhaseFailed,
					metav1.ConditionFalse,
					"TokenRefreshFailed",
					err.Error(),
				)
			}
		}
		run, err := r.Runner.Load(ctx, migration.Status.ExecutionRef)
		if err != nil {
			return ctrl.Result{}, r.setStatus(
				ctx,
				&migration,
				aevrelv1.MigrationPhaseFailed,
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
			if run.Summary().Phase == execution.PhaseSucceeded {
				var latest aevrelv1.CheckpointMigration
				if err := r.Get(ctx, request.NamespacedName, &latest); err != nil {
					return ctrl.Result{}, err
				}
				if latest.Spec.Target.Validation != nil {
					return r.reconcileTargetReady(ctx, &latest)
				}
			}
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
			migration.Status.Phase == aevrelv1.MigrationPhaseSucceeded {
			return ctrl.Result{}, nil
		}
		prepared, err := r.Orchestrator.Prepare(ctx, &migration)
		if err != nil {
			return ctrl.Result{}, r.setStatus(
				ctx,
				&migration,
				aevrelv1.MigrationPhaseFailed,
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
					aevrelv1.MigrationPhaseFailed,
					metav1.ConditionFalse,
					"DispatchFailed",
					err.Error(),
				)
			}
			if result.Committed {
				if err := r.setDispatchSucceeded(
					ctx,
					&migration,
					prepared,
					result,
				); err != nil {
					return ctrl.Result{}, err
				}
				if migration.Spec.Target.Validation != nil {
					return r.reconcileTargetReady(ctx, &migration)
				}
				return ctrl.Result{}, nil
			}
		}
		return ctrl.Result{}, r.setPlanReady(ctx, &migration, prepared)
	}
	return ctrl.Result{}, r.setStatus(
		ctx,
		&migration,
		aevrelv1.MigrationPhasePending,
		metav1.ConditionTrue,
		"Validated",
		"migration spec is valid; waiting for inventory and planning",
	)
}

func (r *Reconciler) setDispatchSucceeded(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	prepared Prepared,
	result DispatchResult,
) error {
	hasValidation := migration.Spec.Target.Validation != nil
	migration.Status.Phase = aevrelv1.MigrationPhaseSucceeded
	if hasValidation {
		migration.Status.Phase = aevrelv1.MigrationPhaseTargetValidating
	}
	migration.Status.PlanRef = prepared.PlanRef
	migration.Status.PlanHash = prepared.PlanHash
	migration.Status.ReportRef = result.ReportRef
	migration.Status.ProofRef = result.ProofRef
	migration.Status.ProofHash = result.ProofHash
	migration.Status.CommitToken = migrationUID(migration)
	if !hasValidation {
		migration.Status.CutoverPhase = "promoted"
	}
	migration.Status.Counts = aevrelv1.RankCounts{
		Committed: int32(len(result.Tasks)),
	}
	if migration.Status.StartedAt == "" {
		migration.Status.StartedAt = time.Now().UTC().Format(time.RFC3339)
	}
	if !hasValidation {
		migration.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
	}
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
	if hasValidation {
		meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
			Type:               targetReadyCondition,
			Status:             metav1.ConditionUnknown,
			Reason:             "Pending",
			Message:            "waiting for target validation Job",
			ObservedGeneration: migration.Generation,
		})
	}
	if err := r.Status().Update(ctx, migration); err != nil {
		return err
	}
	return nil
}

func (r *Reconciler) setPlanReady(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	prepared Prepared,
) error {
	migration.Status.Phase = aevrelv1.MigrationPhasePending
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
		For(&aevrelv1.CheckpointMigration{}).
		Owns(&batchv1.Job{}).
		Complete(r)
}

func (r *Reconciler) setStatus(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
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
	migration *aevrelv1.CheckpointMigration,
	mutate func(*aevrelv1.CheckpointMigration) bool,
) error {
	key := types.NamespacedName{
		Namespace: migration.Namespace,
		Name:      migration.Name,
	}
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest aevrelv1.CheckpointMigration
		if err := r.Get(ctx, key, &latest); err != nil {
			return err
		}
		if !mutate(&latest) {
			return nil
		}
		return r.Status().Update(ctx, &latest)
	})
}

const (
	cancelAnnotation      = "aevrel.dev/cancel"
	retryAnnotation       = "aevrel.dev/retry"
	purgeTargetAnnotation = "aevrel.dev/purge-target"
	cleanupFinalizer      = "aevrel.dev/cleanup"
)

func (r *Reconciler) handleDeletion(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (ctrl.Result, error) {
	if !containsFinalizer(migration, cleanupFinalizer) {
		return ctrl.Result{}, nil
	}
	if err := r.cleanupMigration(ctx, migration); err != nil {
		return ctrl.Result{}, err
	}
	if err := r.removeFinalizer(ctx, migration, cleanupFinalizer); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (r *Reconciler) cleanupMigration(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) error {
	if migration.Annotations[purgeTargetAnnotation] == "true" {
		if r.Resolver == nil || r.Cleaner == nil {
			return fmt.Errorf("target cleanup is not configured")
		}
		targets, err := r.Resolver.ResolveTarget(ctx, migration)
		if err != nil {
			return fmt.Errorf("resolve target cleanup: %w", err)
		}
		for _, entry := range targets {
			root := strings.ReplaceAll(
				migration.Spec.Target.RootTemplate,
				"{rank}",
				strconv.Itoa(int(entry.Rank)),
			)
			if _, err := r.Cleaner.CleanupTarget(
				ctx,
				entry.Endpoint,
				root,
				true,
			); err != nil {
				return fmt.Errorf(
					"cleanup target rank %d %s: %w",
					entry.Rank,
					root,
					err,
				)
			}
		}
	}
	if r.Artifacts != nil {
		deleter, ok := r.Artifacts.(artifactstore.Deleter)
		if !ok {
			return fmt.Errorf("artifact store does not support deletion")
		}
		refs := []string{
			migration.Status.PlanRef,
			migration.Status.ExecutionRef,
			migration.Status.ReportRef,
			migration.Status.ProofRef,
		}
		if migration.Status.TargetReady != nil {
			refs = append(refs, migration.Status.TargetReady.ProofRef)
		}
		for _, ref := range refs {
			if ref == "" {
				continue
			}
			if err := deleter.Delete(ctx, ref); err != nil {
				return fmt.Errorf("delete artifact %s: %w", ref, err)
			}
		}
	}
	return nil
}

func containsFinalizer(migration *aevrelv1.CheckpointMigration, name string) bool {
	for _, finalizer := range migration.Finalizers {
		if finalizer == name {
			return true
		}
	}
	return false
}

func (r *Reconciler) removeFinalizer(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	name string,
) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest aevrelv1.CheckpointMigration
		if err := r.Get(
			ctx,
			types.NamespacedName{
				Namespace: migration.Namespace,
				Name:      migration.Name,
			},
			&latest,
		); err != nil {
			return err
		}
		filtered := latest.Finalizers[:0]
		for _, finalizer := range latest.Finalizers {
			if finalizer != name {
				filtered = append(filtered, finalizer)
			}
		}
		latest.Finalizers = filtered
		return r.Update(ctx, &latest)
	})
}

func (r *Reconciler) handleControlAnnotations(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (bool, error) {
	if migration.Annotations[cancelAnnotation] == "true" {
		return true, r.handleCancel(ctx, migration)
	}
	if migration.Annotations[retryAnnotation] == "true" {
		return true, r.handleRetry(ctx, migration)
	}
	return false, nil
}

func (r *Reconciler) handleCancel(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) error {
	switch migration.Status.Phase {
	case aevrelv1.MigrationPhaseSucceeded,
		aevrelv1.MigrationPhaseFailed,
		aevrelv1.MigrationPhaseTargetFailed,
		aevrelv1.MigrationPhaseCancelled:
		return r.clearAnnotation(ctx, migration, cancelAnnotation)
	case aevrelv1.MigrationPhaseTargetValidating:
		if err := r.setStatus(
			ctx,
			migration,
			migration.Status.Phase,
			metav1.ConditionFalse,
			"CancelRejected",
			"checkpoint is already committed; target validation is in progress",
		); err != nil {
			return err
		}
		return r.clearAnnotation(ctx, migration, cancelAnnotation)
	}
	var run *execution.Run
	var cancelWarning string
	if r.Runner != nil && migration.Status.PlanHash != "" {
		runID := migration.Namespace + "/" + migration.Name + ":" + migration.Status.PlanHash
		run = r.Runner.Get(runID)
	}
	if run != nil {
		if err := r.Runner.Cancel(ctx, run); err != nil {
			if errors.Is(err, execution.ErrCannotCancel) {
				_ = r.setStatus(
					ctx,
					migration,
					migration.Status.Phase,
					metav1.ConditionFalse,
					"CancelRejected",
					err.Error(),
				)
				return r.clearAnnotation(ctx, migration, cancelAnnotation)
			}
			cancelWarning = err.Error()
		}
	}
	message := "migration cancelled before commit intent"
	if cancelWarning != "" {
		message += "; task cancellation warning: " + cancelWarning
	}
	if err := r.setStatus(
		ctx,
		migration,
		aevrelv1.MigrationPhaseCancelled,
		metav1.ConditionTrue,
		"Cancelled",
		message,
	); err != nil {
		return err
	}
	return r.clearAnnotation(ctx, migration, cancelAnnotation)
}

func (r *Reconciler) handleRetry(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) error {
	if migration.Status.Phase != aevrelv1.MigrationPhaseCommitIncomplete {
		_ = r.setStatus(
			ctx,
			migration,
			migration.Status.Phase,
			metav1.ConditionFalse,
			"RetryUnsupported",
			"retry is only supported for commit-incomplete migrations",
		)
		return r.clearAnnotation(ctx, migration, retryAnnotation)
	}
	if r.Runner == nil || migration.Status.PlanHash == "" {
		return r.clearAnnotation(ctx, migration, retryAnnotation)
	}
	runID := migration.Namespace + "/" + migration.Name + ":" + migration.Status.PlanHash
	run := r.Runner.Get(runID)
	if run == nil {
		return fmt.Errorf("execution run %s is not loaded", runID)
	}
	ranks, err := run.RepairCommitIncomplete()
	if err != nil {
		return err
	}
	r.Runner.Requeue(run, ranks)
	if err := r.setStatus(
		ctx,
		migration,
		aevrelv1.MigrationPhaseCommitting,
		metav1.ConditionTrue,
		"RetryRequested",
		"commit-incomplete repair requested",
	); err != nil {
		return err
	}
	return r.clearAnnotation(ctx, migration, retryAnnotation)
}

func (r *Reconciler) clearAnnotation(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	key string,
) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest aevrelv1.CheckpointMigration
		if err := r.Get(
			ctx,
			types.NamespacedName{
				Namespace: migration.Namespace,
				Name:      migration.Name,
			},
			&latest,
		); err != nil {
			return err
		}
		if latest.Annotations[key] != "true" {
			return nil
		}
		delete(latest.Annotations, key)
		return r.Update(ctx, &latest)
	})
}

func (r *Reconciler) setExecutionStatus(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	run *execution.Run,
	planHash string,
) error {
	summary := run.Summary()
	phase := executionPhase(summary.Phase)
	hasValidation := migration.Spec.Target.Validation != nil
	if summary.Phase == execution.PhaseSucceeded && hasValidation {
		phase = aevrelv1.MigrationPhaseTargetValidating
	}
	err := r.updateStatus(ctx, migration, func(latest *aevrelv1.CheckpointMigration) bool {
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
		latest.Status.Counts = aevrelv1.RankCounts{
			Prepared:  int32(summary.Prepared),
			Staged:    int32(summary.Staged),
			Committed: int32(summary.Committed),
			Failed:    int32(summary.Failed),
		}
		rankStatuses := make([]aevrelv1.RankStatus, 0, len(run.Ranks()))
		for _, state := range run.Ranks() {
			rankStatuses = append(rankStatuses, aevrelv1.RankStatus{
				Rank:     state.Rank,
				Phase:    string(state.Phase),
				TaskID:   state.TaskID,
				Attempts: int32(state.Attempts),
				Error:    state.Error,
			})
		}
		if !reflect.DeepEqual(latest.Status.Ranks, rankStatuses) {
			latest.Status.Ranks = rankStatuses
			changed = true
		}
		if phase != aevrelv1.MigrationPhasePending &&
			latest.Status.StartedAt == "" {
			latest.Status.StartedAt = time.Now().UTC().Format(time.RFC3339)
			changed = true
		}
		if phase == aevrelv1.MigrationPhaseSucceeded ||
			phase == aevrelv1.MigrationPhaseFailed ||
			phase == aevrelv1.MigrationPhaseCancelled {
			if latest.Status.CompletedAt == "" {
				latest.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
				changed = true
			}
		}
		if latest.Status.CutoverPhase == "" &&
			phase != aevrelv1.MigrationPhaseTargetValidating {
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
		if phase == aevrelv1.MigrationPhaseSucceeded ||
			phase == aevrelv1.MigrationPhaseFailed ||
			phase == aevrelv1.MigrationPhaseCancelled {
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
	return err
}

func executionPhase(phase execution.Phase) string {
	switch phase {
	case execution.PhaseSucceeded:
		return aevrelv1.MigrationPhaseSucceeded
	case execution.PhaseFailed:
		return aevrelv1.MigrationPhaseFailed
	case execution.PhaseCancelled:
		return aevrelv1.MigrationPhaseCancelled
	case execution.PhaseCommitting:
		return aevrelv1.MigrationPhaseCommitting
	case execution.PhaseCommitIncomplete:
		return aevrelv1.MigrationPhaseCommitIncomplete
	case execution.PhaseStaged:
		return aevrelv1.MigrationPhaseTransferring
	case execution.PhaseFinalizing:
		return aevrelv1.MigrationPhaseCommitPending
	case execution.PhasePrepared:
		return aevrelv1.MigrationPhasePreparing
	default:
		return aevrelv1.MigrationPhasePending
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

func validateMigration(migration *aevrelv1.CheckpointMigration) error {
	spec := &migration.Spec
	if err := validateMigrationMode(spec); err != nil {
		return err
	}
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
	if spec.Target.Validation != nil && spec.Target.ClusterRef != "" {
		return fmt.Errorf(
			"target.validation does not support target.clusterRef in 0.2",
		)
	}
	settings, enabled, err := parseTargetValidationSettings(migration)
	if err != nil {
		return err
	}
	if enabled {
		if _, err := targetValidationRanks(migration); err != nil {
			return fmt.Errorf("target.validation: %w", err)
		}
		if settings.HasJobTemplate {
			if _, err := renderTargetValidationJob(migration, settings); err != nil {
				return fmt.Errorf("target.validation: %w", err)
			}
		}
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
func ValidateMigration(migration *aevrelv1.CheckpointMigration) error {
	return validateMigration(migration)
}

func validateMesh(role string, worldSize int32, mesh aevrelv1.MeshSpec) error {
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

func validateRanks(role string, worldSize int32, ranks []aevrelv1.RankEndpoint) error {
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
	ranks []aevrelv1.RankEndpoint,
	selector *aevrelv1.AgentSelector,
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
