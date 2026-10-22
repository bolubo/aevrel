package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func TestReadinessCommandShowsStatus(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: shardferryv1.CheckpointRecordSpec{
			JobRef:     "job42",
			Version:    "step-100",
			Provider:   "dcp",
			Checkpoint: "/checkpoints/job42",
		},
		Status: shardferryv1.CheckpointRecordStatus{
			Readiness: shardferryv1.CheckpointReadinessStatus{
				Status:   shardferryv1.ReadinessUnverified,
				Complete: true,
				Fresh:    true,
				Reasons: []shardferryv1.ControlReason{{
					Code:   "DRILL_MISSING",
					Detail: "no restore drill",
				}},
			},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	var output bytes.Buffer
	if err := execute(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"readiness", "job42"},
		&output,
	); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"Status:       UNVERIFIED",
		"Complete:     true",
		"Loadable:     unknown",
		"DRILL_MISSING",
		"Next:",
	} {
		if !strings.Contains(output.String(), want) {
			t.Fatalf("missing %q:\n%s", want, output.String())
		}
	}
}
