package controller

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/artifactstore"
	"github.com/bolubo/aevrel/internal/execution"
)

func TestReconcilerAcceptsValidMigration(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("job42")
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{Client: client, Scheme: scheme}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != string(aevrelv1.MigrationPhasePending) {
		t.Fatalf("phase = %q, want pending", got.Status.Phase)
	}
	if len(got.Status.Conditions) == 0 {
		t.Fatal("expected an Accepted condition")
	}
	if got.Status.Conditions[0].Type != "Accepted" {
		t.Fatalf("condition type = %q, want Accepted", got.Status.Conditions[0].Type)
	}
}

func TestReconcilerRejectsInvalidRankMap(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("bad-rank-map")
	migration.Spec.Target.RankMap = migration.Spec.Target.RankMap[:1]
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{Client: client, Scheme: scheme}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != string(aevrelv1.MigrationPhaseFailed) {
		t.Fatalf("phase = %q, want failed", got.Status.Phase)
	}
}

func TestReconcilerCallsInventoryAndPlanning(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("planned")
	agents := &fakeAgentAPI{
		inventories: map[int32]agentclient.Inventory{
			0: fakeInventory(0),
			1: fakeInventory(1),
		},
		plan: map[string]any{"version": "1", "nodes": []any{}},
	}
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{
		Client:       client,
		Scheme:       scheme,
		Orchestrator: &Orchestrator{Agents: agents},
	}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.PlanHash == "" {
		t.Fatal("plan hash was not written")
	}
	foundPlanReady := false
	for _, condition := range got.Status.Conditions {
		if condition.Type == "PlanReady" && condition.Status == metav1.ConditionTrue {
			foundPlanReady = true
		}
	}
	if !foundPlanReady {
		t.Fatal("PlanReady condition was not written")
	}
}

func TestReconcilerDispatchesAndCommitsAllRanks(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("dispatch")
	agents := &fakeAgentAPI{
		inventories: map[int32]agentclient.Inventory{
			0: fakeInventory(0),
			1: fakeInventory(1),
		},
		plan: map[string]any{"version": "1", "nodes": []any{}},
	}
	tasks := &fakeTaskAPI{
		tasks:    map[string]agentclient.Task{},
		failRank: -1,
	}
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{
		Client: client,
		Scheme: scheme,
		Orchestrator: &Orchestrator{
			Agents:    agents,
			Tasks:     tasks,
			Artifacts: artifactstore.NewMemoryStore("http://controller:8082"),
		},
	}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != aevrelv1.MigrationPhaseSucceeded {
		t.Fatalf("phase = %q, want succeeded", got.Status.Phase)
	}
	if len(tasks.committed) != 2 {
		t.Fatalf("committed tasks = %d, want 2", len(tasks.committed))
	}
}

func TestReconcilerRestoresExecutionSnapshot(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("restore")
	migration.Status.Phase = aevrelv1.MigrationPhasePending
	migration.Status.PlanHash = "sha256:" + strings.Repeat("a", 64)

	run := execution.NewRun(execution.RunConfig{
		ID:           "restore-run",
		PlanID:       migration.Status.PlanHash,
		MigrationUID: "restore",
		TargetWorld:  2,
		SpecRef:      "http://artifact/spec",
		PlanRef:      "http://artifact/plan",
		RootTemplate: "/target/rank{rank}",
		Plan:         map[string]any{"nodes": []any{}},
		Endpoints: map[int32]string{
			0: "http://agent-0:9443",
			1: "http://agent-1:9443",
		},
	})
	for rank := int32(0); rank < 2; rank++ {
		run.RegisterRank(rank)
		run.SetPhase(rank, execution.PhaseCommitted, "task", "")
	}
	run.SetProof(
		"http://controller:8082/v1/artifacts/proof",
		"sha256:"+strings.Repeat("c", 64),
	)
	snapshot, err := json.Marshal(run.Snapshot())
	if err != nil {
		t.Fatal(err)
	}
	store := artifactstore.NewMemoryStore("http://controller:8082")
	ref, err := store.Put(context.Background(), snapshot)
	if err != nil {
		t.Fatal(err)
	}
	migration.Status.ExecutionRef = ref.URL

	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	runner := execution.NewRunner(nil, 1)
	runner.SetArtifacts(store)
	reconciler := &Reconciler{
		Client: client,
		Scheme: scheme,
		Runner: runner,
	}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != aevrelv1.MigrationPhaseSucceeded {
		t.Fatalf("phase = %q, want succeeded", got.Status.Phase)
	}
	if got.Status.ExecutionRef == "" {
		t.Fatal("execution ref was not restored")
	}
	if got.Status.ProofRef == "" ||
		got.Status.ProofHash != "sha256:"+strings.Repeat("c", 64) {
		t.Fatalf(
			"proof status = %q %q",
			got.Status.ProofRef,
			got.Status.ProofHash,
		)
	}
}

func TestReconcilerCancelsPendingMigrationAndClearsAnnotation(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("cancel-me")
	migration.Annotations = map[string]string{
		"aevrel.dev/cancel": "true",
	}
	client := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{Client: client, Scheme: scheme}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got aevrelv1.CheckpointMigration
	if err := client.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != aevrelv1.MigrationPhaseCancelled {
		t.Fatalf("phase = %q, want cancelled", got.Status.Phase)
	}
	if got.Annotations["aevrel.dev/cancel"] == "true" {
		t.Fatal("cancel annotation was not cleared")
	}
}

func validMigration(name string) *aevrelv1.CheckpointMigration {
	return &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "default",
			Name:      name,
		},
		Spec: aevrelv1.CheckpointMigrationSpec{
			Checkpoint: aevrelv1.CheckpointSpec{
				Format:        "dcp",
				FormatVersion: "1.0.0",
			},
			Source: aevrelv1.SourceSpec{
				MetadataOwnerRank: 0,
				WorldSize:         2,
				Mesh: aevrelv1.MeshSpec{
					Shape:      []int32{2},
					Placements: []string{"Shard(0)"},
				},
				RankMap: []aevrelv1.RankEndpoint{
					{Rank: 0, Endpoint: "https://source-0:9443"},
					{Rank: 1, Endpoint: "https://source-0:9443"},
				},
				CheckpointRoot: "/checkpoints/job42",
			},
			Target: aevrelv1.TargetSpec{
				WorldSize: 2,
				Mesh: aevrelv1.MeshSpec{
					Shape:      []int32{2},
					Placements: []string{"Shard(0)"},
				},
				RankMap: []aevrelv1.RankEndpoint{
					{Rank: 0, Endpoint: "https://target-0:9443"},
					{Rank: 1, Endpoint: "https://target-0:9443"},
				},
				RootTemplate: "/checkpoints/job42-migrated/rank{rank}",
			},
			Policy: aevrelv1.PolicySpec{
				Verify:                 "bitwise",
				Commit:                 "all-or-nothing",
				MaxConcurrentTransfers: 16,
				MaxAttemptsPerRank:     5,
				TimeoutSeconds:         3600,
				SourceChunkCacheBytes:  2147483648,
			},
			Artifacts: aevrelv1.ArtifactSpec{
				Store:     "pvc",
				ClaimName: "aevrel-artifacts",
			},
		},
	}
}
