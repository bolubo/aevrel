package controller

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

const mobilityTransactionPollInterval = 30 * time.Second

type mobilityTransactionConfigError struct {
	err error
}

func (e *mobilityTransactionConfigError) Error() string {
	return e.err.Error()
}

func (e *mobilityTransactionConfigError) Unwrap() error {
	return e.err
}

func mobilityConfigError(err error) error {
	return &mobilityTransactionConfigError{err: err}
}

type MobilityTransactionReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *MobilityTransactionReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var transaction shardferryv1.MobilityTransaction
	if err := r.Get(ctx, request.NamespacedName, &transaction); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if transaction.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	var migration shardferryv1.CheckpointMigration
	migrationName := transaction.Spec.MigrationRef
	if transaction.Spec.MigrationSpec != nil {
		if migrationName != "" {
			return r.failMobilityConfig(
				ctx,
				&transaction,
				"migrationRef and migrationSpec are mutually exclusive",
			)
		}
		owned, err := r.ensureOwnedMigration(ctx, &transaction)
		if err != nil {
			var configErr *mobilityTransactionConfigError
			if errors.As(err, &configErr) {
				return r.failMobilityConfig(ctx, &transaction, configErr.Error())
			}
			return ctrl.Result{}, err
		}
		migration = *owned
		migrationName = owned.Name
	} else {
		if migrationName == "" {
			migrationName = transaction.Name
		}
		if err := r.Get(ctx, client.ObjectKey{
			Namespace: transaction.Namespace,
			Name:      migrationName,
		}, &migration); err != nil {
			if apierrors.IsNotFound(err) {
				result, writeErr := r.writeMobilityStatus(
					ctx,
					&transaction,
					shardferryv1.TransactionPending,
					"",
					fmt.Sprintf(
						"waiting for CheckpointMigration %q",
						migrationName,
					),
					nil,
					metav1.ConditionUnknown,
				)
				if writeErr != nil {
					return ctrl.Result{}, writeErr
				}
				result.RequeueAfter = mobilityTransactionPollInterval
				return result, nil
			}
			return ctrl.Result{}, err
		}
	}
	migrationMode := mobilityMode(&migration)
	if transaction.Spec.Mode != "" &&
		transaction.Spec.Mode != migrationMode {
		return r.writeMobilityStatus(
			ctx,
			&transaction,
			shardferryv1.TransactionFailed,
			"",
			fmt.Sprintf(
				"transaction mode %q does not match migration mode %q",
				transaction.Spec.Mode,
				migrationMode,
			),
			&migration,
			metav1.ConditionFalse,
		)
	}
	phase := mobilityPhase(migration.Status.Phase)
	message := ""
	if migration.Status.TargetReady != nil {
		message = migration.Status.TargetReady.Message
	}
	if message == "" {
		message = "checkpoint migration phase " + migration.Status.Phase
	}
	condition := metav1.ConditionUnknown
	if phase == shardferryv1.TransactionSucceeded {
		condition = metav1.ConditionTrue
	}
	if phase == shardferryv1.TransactionFailed ||
		phase == shardferryv1.TransactionCancelled ||
		phase == shardferryv1.TransactionCommitIncomplete {
		condition = metav1.ConditionFalse
	}
	result, err := r.writeMobilityStatus(
		ctx,
		&transaction,
		phase,
		migration.Status.ProofRef,
		message,
		&migration,
		condition,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !mobilityTerminal(phase) {
		result.RequeueAfter = mobilityTransactionPollInterval
	}
	return result, nil
}

func (r *MobilityTransactionReconciler) ensureOwnedMigration(
	ctx context.Context,
	transaction *shardferryv1.MobilityTransaction,
) (*shardferryv1.CheckpointMigration, error) {
	desiredMode := mobilityModeSpec(transaction.Spec.MigrationSpec)
	if transaction.Spec.Mode != "" &&
		transaction.Spec.Mode != desiredMode {
		return nil, mobilityConfigError(fmt.Errorf(
			"transaction mode %q does not match migrationSpec mode %q",
			transaction.Spec.Mode,
			desiredMode,
		))
	}
	desired := &shardferryv1.CheckpointMigration{
		TypeMeta: metav1.TypeMeta{
			APIVersion: shardferryv1.GroupVersion.String(),
			Kind:       "CheckpointMigration",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      transaction.Name,
			Namespace: transaction.Namespace,
			Labels: map[string]string{
				"app.kubernetes.io/managed-by":       "shardferry",
				"shardferry.io/mobility-transaction": transaction.Name,
			},
			Annotations: map[string]string{
				"shardferry.io/mobility-transaction-uid": string(transaction.UID),
			},
		},
		Spec: *transaction.Spec.MigrationSpec.DeepCopy(),
	}
	if desired.Spec.Mode == "" {
		desired.Spec.Mode = migrationModeForTransaction(desiredMode)
	}
	if transaction.UID != "" {
		controller := true
		blockOwnerDeletion := true
		desired.OwnerReferences = []metav1.OwnerReference{{
			APIVersion:         shardferryv1.GroupVersion.String(),
			Kind:               "MobilityTransaction",
			Name:               transaction.Name,
			UID:                transaction.UID,
			Controller:         &controller,
			BlockOwnerDeletion: &blockOwnerDeletion,
		}}
	}
	if err := r.Create(ctx, desired); err != nil {
		if !apierrors.IsAlreadyExists(err) {
			return nil, err
		}
		var existing shardferryv1.CheckpointMigration
		if err := r.Get(ctx, client.ObjectKey{
			Namespace: transaction.Namespace,
			Name:      transaction.Name,
		}, &existing); err != nil {
			return nil, err
		}
		if !ownedByMobilityTransaction(&existing, transaction) {
			return nil, mobilityConfigError(fmt.Errorf(
				"CheckpointMigration %q already exists and is not controlled by MobilityTransaction %q",
				existing.Name,
				transaction.Name,
			))
		}
		return &existing, nil
	}
	return desired, nil
}

func (r *MobilityTransactionReconciler) failMobilityConfig(
	ctx context.Context,
	transaction *shardferryv1.MobilityTransaction,
	message string,
) (ctrl.Result, error) {
	return r.writeMobilityStatus(
		ctx,
		transaction,
		shardferryv1.TransactionFailed,
		"",
		message,
		nil,
		metav1.ConditionFalse,
	)
}

func (r *MobilityTransactionReconciler) writeMobilityStatus(
	ctx context.Context,
	transaction *shardferryv1.MobilityTransaction,
	phase string,
	proofRef string,
	message string,
	migration *shardferryv1.CheckpointMigration,
	conditionStatus metav1.ConditionStatus,
) (ctrl.Result, error) {
	before := transaction.Status.DeepCopy()
	transaction.Status.Phase = phase
	transaction.Status.ProofRef = proofRef
	transaction.Status.Message = message
	if migration != nil {
		transaction.Status.MigrationPhase = migration.Status.Phase
		if migration.Status.TargetReady != nil {
			transaction.Status.TargetReady = migration.Status.TargetReady.DeepCopy()
		} else {
			transaction.Status.TargetReady = nil
		}
	} else {
		transaction.Status.MigrationPhase = ""
		transaction.Status.TargetReady = nil
	}
	reason := phase
	if reason == "" {
		reason = "Waiting"
	}
	meta.SetStatusCondition(&transaction.Status.Conditions, metav1.Condition{
		Type:               "MobilityTransactionReady",
		Status:             conditionStatus,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: transaction.Generation,
	})
	if reflect.DeepEqual(before, &transaction.Status) {
		return ctrl.Result{}, nil
	}
	if err := r.Status().Update(ctx, transaction); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (r *MobilityTransactionReconciler) SetupWithManager(
	mgr ctrl.Manager,
) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&shardferryv1.MobilityTransaction{}).
		Watches(
			&shardferryv1.CheckpointMigration{},
			handler.EnqueueRequestsFromMapFunc(r.requestsForMigration),
		).
		Complete(r)
}

func (r *MobilityTransactionReconciler) requestsForMigration(
	ctx context.Context,
	object client.Object,
) []reconcile.Request {
	migration, ok := object.(*shardferryv1.CheckpointMigration)
	if !ok {
		return nil
	}
	var transactions shardferryv1.MobilityTransactionList
	if err := r.List(
		ctx,
		&transactions,
		client.InNamespace(migration.Namespace),
	); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for _, transaction := range transactions.Items {
		if transaction.Spec.MigrationRef == migration.Name ||
			(transaction.Spec.MigrationRef == "" &&
				transaction.Name == migration.Name) {
			requests = append(requests, reconcile.Request{
				NamespacedName: client.ObjectKeyFromObject(&transaction),
			})
		}
	}
	return requests
}

func mobilityMode(migration *shardferryv1.CheckpointMigration) string {
	switch migration.Spec.Mode {
	case "", shardferryv1.MigrationModeStatic:
		return shardferryv1.TransactionModeStatic
	case shardferryv1.MigrationModeLiveRankSplit:
		return shardferryv1.TransactionModeLive
	default:
		return migration.Spec.Mode
	}
}

func mobilityModeSpec(spec *shardferryv1.CheckpointMigrationSpec) string {
	if spec == nil || spec.Mode == "" {
		return shardferryv1.TransactionModeStatic
	}
	switch spec.Mode {
	case shardferryv1.MigrationModeStatic:
		return shardferryv1.TransactionModeStatic
	case shardferryv1.MigrationModeLiveRankSplit:
		return shardferryv1.TransactionModeLive
	default:
		return spec.Mode
	}
}

func migrationModeForTransaction(mode string) string {
	if mode == shardferryv1.TransactionModeLive {
		return shardferryv1.MigrationModeLiveRankSplit
	}
	return shardferryv1.MigrationModeStatic
}

func ownedByMobilityTransaction(
	migration *shardferryv1.CheckpointMigration,
	transaction *shardferryv1.MobilityTransaction,
) bool {
	if transaction.UID == "" {
		return migration.Labels["shardferry.io/mobility-transaction"] ==
			transaction.Name
	}
	for _, owner := range migration.OwnerReferences {
		if owner.APIVersion == shardferryv1.GroupVersion.String() &&
			owner.Kind == "MobilityTransaction" &&
			owner.UID == transaction.UID &&
			owner.Controller != nil &&
			*owner.Controller {
			return true
		}
	}
	return false
}

func mobilityPhase(phase string) string {
	switch phase {
	case shardferryv1.MigrationPhasePending, "":
		return shardferryv1.TransactionPending
	case shardferryv1.MigrationPhasePreparing:
		return shardferryv1.TransactionPreparing
	case shardferryv1.MigrationPhaseTransferring:
		return shardferryv1.TransactionTransferring
	case shardferryv1.MigrationPhaseCommitPending,
		shardferryv1.MigrationPhaseTargetValidating:
		return shardferryv1.TransactionVerifying
	case shardferryv1.MigrationPhaseCommitting:
		return shardferryv1.TransactionCommitting
	case shardferryv1.MigrationPhaseSucceeded:
		return shardferryv1.TransactionSucceeded
	case shardferryv1.MigrationPhaseFailed,
		shardferryv1.MigrationPhaseTargetFailed:
		return shardferryv1.TransactionFailed
	case shardferryv1.MigrationPhaseCancelled:
		return shardferryv1.TransactionCancelled
	case shardferryv1.MigrationPhaseCommitIncomplete:
		return shardferryv1.TransactionCommitIncomplete
	default:
		return shardferryv1.TransactionPending
	}
}

func mobilityTerminal(phase string) bool {
	return phase == shardferryv1.TransactionSucceeded ||
		phase == shardferryv1.TransactionFailed ||
		phase == shardferryv1.TransactionCancelled ||
		phase == shardferryv1.TransactionCommitIncomplete
}
