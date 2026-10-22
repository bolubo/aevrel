package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/shardferry/shardferry/internal/kueueadapter"
)

func TestExecuteKueueStatusAndExplain(t *testing.T) {
	workload := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": kueueadapter.DefaultWorkloadGVK.GroupVersion().String(),
		"kind":       kueueadapter.DefaultWorkloadGVK.Kind,
		"metadata": map[string]any{
			"name":      "training",
			"namespace": "default",
			"labels": map[string]any{
				kueueadapter.LabelPreemptionProtected: "1",
				kueueadapter.LabelPreemptionReason:    "CheckpointIncomplete",
				kueueadapter.LabelProgressPercent:     "42",
				kueueadapter.LabelCheckpointComplete:  "0",
				kueueadapter.LabelDecisionSource:      "checkpoint-record",
			},
		},
		"status": map[string]any{
			"conditions": []any{
				map[string]any{
					"type":   "Admitted",
					"status": "True",
					"reason": "ByQuota",
				},
			},
		},
	}}
	scheme := runtime.NewScheme()
	kueueadapter.AddToScheme(scheme, kueueadapter.DefaultWorkloadGVK)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(workload).
		Build()

	var out bytes.Buffer
	if err := executeKueue(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"status"},
		&out,
	); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	for _, wanted := range []string{
		"training",
		"CheckpointIncomplete",
		"checkpoint-record",
		"Admitted",
	} {
		if !strings.Contains(text, wanted) {
			t.Fatalf("status missing %q:\n%s", wanted, text)
		}
	}

	out.Reset()
	if err := executeKueue(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"explain", "training"},
		&out,
	); err != nil {
		t.Fatal(err)
	}
	text = out.String()
	for _, wanted := range []string{
		"Protected:    1",
		"Reason:       CheckpointIncomplete",
		"Condition:    Admitted=True ByQuota",
		"Next: inspect",
	} {
		if !strings.Contains(text, wanted) {
			t.Fatalf("explain missing %q:\n%s", wanted, text)
		}
	}
}
