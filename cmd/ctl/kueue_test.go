package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/bolubo/aevrel/internal/control"
	"github.com/bolubo/aevrel/internal/kueueadapter"
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

// movable is a two-field check. Printing it alone is how a consumer reads it
// as "this checkpoint will run at the target size", so the explanation carries
// the qualifier whenever the fact is true — and the topology caveat only when
// the assessment actually raised TARGET_TOPOLOGY_UNVERIFIED.
func TestKueueExplainQualifiesMovable(t *testing.T) {
	movable := true
	var out bytes.Buffer
	writeMovableCaveat(&out, kueueadapter.CheckpointFacts{Movable: &movable})
	text := out.String()
	if !strings.Contains(text, "coarse filter, not a topology verdict") {
		t.Fatalf("movable caveat missing:/n%s", text)
	}
	if strings.Contains(text, "target world size differs") {
		t.Fatalf("topology caveat must not fire without the reason code:/n%s", text)
	}

	out.Reset()
	writeMovableCaveat(&out, kueueadapter.CheckpointFacts{
		Movable:     &movable,
		ReasonCodes: []string{control.ReasonTargetTopologyUnverified},
	})
	text = out.String()
	if !strings.Contains(text, "target world size differs from the source's rank count") {
		t.Fatalf("topology caveat missing:/n%s", text)
	}

	// movable=null means "not evaluated", which is neither movable nor a
	// migration guarantee: there is nothing to qualify.
	out.Reset()
	writeMovableCaveat(&out, kueueadapter.CheckpointFacts{})
	if out.Len() != 0 {
		t.Fatalf("an unevaluated movable must print nothing:/n%s", out.String())
	}
}
