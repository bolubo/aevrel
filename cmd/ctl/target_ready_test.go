package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func TestDescribeShowsTargetReady(t *testing.T) {
	migration := &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Status: aevrelv1.CheckpointMigrationStatus{
			Phase: aevrelv1.MigrationPhaseTargetFailed,
			TargetReady: &aevrelv1.TargetReadyStatus{
				Ready:         false,
				Condition:     "Incomplete",
				Version:       "step-100000",
				RanksReady:    1,
				RanksTotal:    2,
				StepsRan:      10,
				StateComplete: false,
				ProofRef:      "http://controller/artifacts/target",
				ProofHash:     "sha256:abc",
				Ranks: []aevrelv1.TargetReadyRankStatus{{
					Rank:            0,
					ModelLoaded:     true,
					OptimizerLoaded: true,
					StepsRan:        10,
					OK:              true,
				}},
			},
		},
	}
	var output bytes.Buffer
	if err := writeDescribe(&output, migration, true); err != nil {
		t.Fatal(err)
	}
	text := output.String()
	for _, want := range []string{
		"Next:",
		"TargetReady: ready=false condition=Incomplete",
		"version=step-100000 ranks=1/2",
		"Target proof: http://controller/artifacts/target",
		"TARGET_RANK",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("describe missing %q:\n%s", want, text)
		}
	}
}

func TestWaitForMigrationStopsOnTargetFailed(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Status: aevrelv1.CheckpointMigrationStatus{
			Phase: aevrelv1.MigrationPhaseTargetFailed,
			Conditions: []metav1.Condition{{
				Type:    "TargetReady",
				Status:  metav1.ConditionFalse,
				Reason:  "Incomplete",
				Message: "missing target-ready reports",
			}},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	var output bytes.Buffer
	err := waitForMigration(
		context.Background(),
		kubeClient,
		migration,
		migrateOptions{timeout: time.Second},
		false,
		&output,
	)
	if err == nil || !strings.Contains(err.Error(), "missing target-ready reports") {
		t.Fatalf("error = %v", err)
	}
}
