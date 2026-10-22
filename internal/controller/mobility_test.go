package controller

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func TestMobilityTransactionProjectsMigrationLifecycle(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.CheckpointMigrationSpec{
			Mode: shardferryv1.MigrationModeStatic,
		},
		Status: shardferryv1.CheckpointMigrationStatus{
			Phase:    shardferryv1.MigrationPhaseSucceeded,
			ProofRef: "proof://migration/training-move",
			TargetReady: &shardferryv1.TargetReadyStatus{
				Condition:  "Ready",
				Ready:      true,
				RanksReady: 2,
				RanksTotal: 2,
				StepsRan:   10,
				ProofRef:   "proof://target-ready",
				Message:    "target resumed",
			},
		},
	}
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move-view", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			MigrationRef: "training-move",
			RecordRef:    "training-step-100",
			Mode:         shardferryv1.TransactionModeStatic,
			Source:       "cluster-a",
			Target:       "cluster-b",
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(migration, transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move-view",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{
			Namespace: "default",
			Name:      "training-move-view",
		},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionSucceeded ||
		got.Status.MigrationPhase != shardferryv1.MigrationPhaseSucceeded ||
		got.Status.ProofRef != "proof://migration/training-move" {
		t.Fatalf("transaction status = %+v", got.Status)
	}
	if got.Status.TargetReady == nil ||
		got.Status.TargetReady.RanksReady != 2 ||
		got.Status.TargetReady.Message != "target resumed" {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
	condition := meta.FindStatusCondition(
		got.Status.Conditions,
		"MobilityTransactionReady",
	)
	if condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("condition = %+v", condition)
	}
}

func TestMobilityTransactionCreatesOwnedMigration(t *testing.T) {
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-move",
			Namespace: "default",
			UID:       types.UID("transaction-uid"),
		},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeStatic,
			Source:    "cluster-a",
			Target:    "cluster-b",
			MigrationSpec: &shardferryv1.CheckpointMigrationSpec{
				Mode: shardferryv1.MigrationModeStatic,
			},
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var migration shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&migration,
	); err != nil {
		t.Fatal(err)
	}
	if migration.Spec.Mode != shardferryv1.MigrationModeStatic {
		t.Fatalf("migration mode = %q", migration.Spec.Mode)
	}
	if len(migration.OwnerReferences) != 1 ||
		migration.OwnerReferences[0].Kind != "MobilityTransaction" ||
		migration.OwnerReferences[0].UID != transaction.UID {
		t.Fatalf("ownerReferences = %+v", migration.OwnerReferences)
	}
	if migration.Labels["shardferry.io/mobility-transaction"] != "training-move" {
		t.Fatalf("migration labels = %+v", migration.Labels)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionPending {
		t.Fatalf("transaction phase = %q", got.Status.Phase)
	}
}

func TestMobilityTransactionRejectsUnownedMigration(t *testing.T) {
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-move",
			Namespace: "default",
			UID:       types.UID("transaction-uid"),
		},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeStatic,
			Source:    "cluster-a",
			Target:    "cluster-b",
			MigrationSpec: &shardferryv1.CheckpointMigrationSpec{
				Mode: shardferryv1.MigrationModeStatic,
			},
		},
	}
	existing := &shardferryv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-move",
			Namespace: "default",
		},
		Spec: shardferryv1.CheckpointMigrationSpec{
			Mode: shardferryv1.MigrationModeStatic,
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(transaction, existing).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionFailed ||
		!strings.Contains(got.Status.Message, "not controlled") {
		t.Fatalf("transaction status = %+v", got.Status)
	}
}

func TestMobilityTransactionCreatesLiveMigration(t *testing.T) {
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "live-move",
			Namespace: "default",
			UID:       types.UID("transaction-uid"),
		},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeLive,
			Source:    "cluster-a",
			Target:    "cluster-b",
			MigrationSpec: &shardferryv1.CheckpointMigrationSpec{
				Mode: shardferryv1.MigrationModeLiveRankSplit,
			},
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "live-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var migration shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "live-move"},
		&migration,
	); err != nil {
		t.Fatal(err)
	}
	if migration.Spec.Mode != shardferryv1.MigrationModeLiveRankSplit {
		t.Fatalf("migration mode = %q", migration.Spec.Mode)
	}
}

func TestMobilityTransactionRejectsReferenceAndSpecTogether(t *testing.T) {
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "ambiguous", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef:    "training-step-100",
			MigrationRef: "existing",
			MigrationSpec: &shardferryv1.CheckpointMigrationSpec{
				Mode: shardferryv1.MigrationModeStatic,
			},
			Mode:   shardferryv1.TransactionModeStatic,
			Source: "cluster-a",
			Target: "cluster-b",
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "ambiguous",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "ambiguous"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionFailed ||
		!strings.Contains(got.Status.Message, "mutually exclusive") {
		t.Fatalf("transaction status = %+v", got.Status)
	}
}

func TestMobilityTransactionFailsClosedOnTargetFailure(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.CheckpointMigrationSpec{
			Mode: shardferryv1.MigrationModeStatic,
		},
		Status: shardferryv1.CheckpointMigrationStatus{
			Phase: shardferryv1.MigrationPhaseTargetFailed,
			TargetReady: &shardferryv1.TargetReadyStatus{
				Condition: "LoadFailed",
				Ready:     false,
				Message:   "target did not load optimizer",
			},
		},
	}
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeStatic,
			Source:    "cluster-a",
			Target:    "cluster-b",
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(migration, transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionFailed ||
		got.Status.Message != "target did not load optimizer" {
		t.Fatalf("transaction status = %+v", got.Status)
	}
	condition := meta.FindStatusCondition(
		got.Status.Conditions,
		"MobilityTransactionReady",
	)
	if condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("condition = %+v", condition)
	}
}

func TestMobilityTransactionWaitsForMigration(t *testing.T) {
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "missing-view", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			MigrationRef: "missing",
			RecordRef:    "training-step-100",
			Mode:         shardferryv1.TransactionModeStatic,
			Source:       "cluster-a",
			Target:       "cluster-b",
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	result, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "missing-view",
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter != mobilityTransactionPollInterval {
		t.Fatalf("requeueAfter = %s", result.RequeueAfter)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "missing-view"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionPending ||
		got.Status.Message != `waiting for CheckpointMigration "missing"` {
		t.Fatalf("transaction status = %+v", got.Status)
	}
}

func TestMobilityTransactionRejectsModeMismatch(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.CheckpointMigrationSpec{
			Mode: shardferryv1.MigrationModeLiveRankSplit,
		},
	}
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeStatic,
			Source:    "cluster-a",
			Target:    "cluster-b",
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(migration, transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != shardferryv1.TransactionFailed {
		t.Fatalf("transaction status = %+v", got.Status)
	}
}

func TestMobilityTransactionClearsStaleTargetReady(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.CheckpointMigrationSpec{
			Mode: shardferryv1.MigrationModeStatic,
		},
		Status: shardferryv1.CheckpointMigrationStatus{
			Phase: shardferryv1.MigrationPhaseTransferring,
		},
	}
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef: "training-step-100",
			Mode:      shardferryv1.TransactionModeStatic,
			Source:    "cluster-a",
			Target:    "cluster-b",
		},
		Status: shardferryv1.MobilityTransactionStatus{
			TargetReady: &shardferryv1.TargetReadyStatus{Ready: true},
		},
	}
	scheme := mobilityScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.MobilityTransaction{}).
		WithObjects(migration, transaction).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-move",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.MobilityTransaction
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-move"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.TargetReady != nil {
		t.Fatalf("stale targetReady = %+v", got.Status.TargetReady)
	}
	if got.Status.Phase != shardferryv1.TransactionTransferring {
		t.Fatalf("phase = %q", got.Status.Phase)
	}
}

func TestMobilityTransactionWatchesReferencedMigrations(t *testing.T) {
	transactions := []shardferryv1.MobilityTransaction{
		{
			ObjectMeta: metav1.ObjectMeta{Name: "view-a", Namespace: "default"},
			Spec: shardferryv1.MobilityTransactionSpec{
				MigrationRef: "training-move",
			},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "training-move", Namespace: "default"},
		},
		{
			ObjectMeta: metav1.ObjectMeta{Name: "unrelated", Namespace: "default"},
			Spec: shardferryv1.MobilityTransactionSpec{
				MigrationRef: "other-migration",
			},
		},
	}
	scheme := mobilityScheme(t)
	objects := make([]runtime.Object, 0, len(transactions))
	for index := range transactions {
		objects = append(objects, &transactions[index])
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objects...).
		Build()
	reconciler := &MobilityTransactionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	requests := reconciler.requestsForMigration(
		context.Background(),
		&shardferryv1.CheckpointMigration{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "training-move",
				Namespace: "default",
			},
		},
	)
	if len(requests) != 2 {
		t.Fatalf("requests = %+v", requests)
	}
}

func mobilityScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return scheme
}
