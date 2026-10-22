package controller

import (
	"context"
	"sync"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

type fakeCutoverManager struct {
	mu         sync.Mutex
	follows    int
	promotes   []string
	recoveries int
	state      string
}

func (f *fakeCutoverManager) Status(context.Context) (map[string]any, error) {
	return map[string]any{
		"state":         f.state,
		"source_latest": "step-200",
	}, nil
}

func (f *fakeCutoverManager) Follow(context.Context) ([]map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.follows++
	return nil, nil
}

func (f *fakeCutoverManager) Cutover(_ context.Context, version string) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.promotes = append(f.promotes, version)
	f.state = "promoted"
	return map[string]any{"state": "promoted", "version_id": version}, nil
}

func (f *fakeCutoverManager) Recover(context.Context) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.recoveries++
	return map[string]any{"state": "promoted"}, nil
}

func TestReconcilerObservesCutoverService(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("cutover-observe")
	migration.Status.Phase = shardferryv1.MigrationPhaseSucceeded
	migration.Status.CutoverPhase = "following"
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	manager := &fakeCutoverManager{state: "catching-up"}
	reconciler := &Reconciler{
		Client:  kubeClient,
		Scheme:  scheme,
		Cutover: manager,
	}
	if err := reconciler.reconcileCutover(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		clientKey(migration),
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.CutoverPhase != "catching-up" {
		t.Fatalf("cutover phase = %q", got.Status.CutoverPhase)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, "CutoverReady")
	if condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("CutoverReady condition = %+v", condition)
	}
	if manager.follows != 0 || len(manager.promotes) != 0 {
		t.Fatalf("unexpected mutating calls: %+v", manager)
	}
}

func TestReconcilerCanAutoPromoteCutover(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("cutover-auto")
	migration.Status.Phase = shardferryv1.MigrationPhaseSucceeded
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	manager := &fakeCutoverManager{state: "catching-up"}
	reconciler := &Reconciler{
		Client:             kubeClient,
		Scheme:             scheme,
		Cutover:            manager,
		CutoverAutoPromote: true,
	}
	if err := reconciler.reconcileCutover(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	if manager.follows != 1 {
		t.Fatalf("follow calls = %d, want 1", manager.follows)
	}
	if len(manager.promotes) != 1 || manager.promotes[0] != "step-200" {
		t.Fatalf("promotes = %v", manager.promotes)
	}
	var got shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		clientKey(migration),
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.CutoverPhase != "promoted" {
		t.Fatalf("cutover phase = %q", got.Status.CutoverPhase)
	}
}

func TestReconcilerRecoversIncompleteCutover(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("cutover-recover")
	migration.Status.Phase = shardferryv1.MigrationPhaseSucceeded
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	manager := &fakeCutoverManager{state: "cutover-incomplete"}
	reconciler := &Reconciler{
		Client:  kubeClient,
		Scheme:  scheme,
		Cutover: manager,
	}
	if err := reconciler.reconcileCutover(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	if manager.recoveries != 1 {
		t.Fatalf("recover calls = %d, want 1", manager.recoveries)
	}
	var got shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		clientKey(migration),
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.CutoverPhase != "promoted" {
		t.Fatalf("cutover phase = %q, want promoted", got.Status.CutoverPhase)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, "CutoverReady")
	if condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("CutoverReady condition = %+v", condition)
	}
}

func clientKey(migration *shardferryv1.CheckpointMigration) types.NamespacedName {
	return types.NamespacedName{
		Namespace: migration.Namespace,
		Name:      migration.Name,
	}
}
