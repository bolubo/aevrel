package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func TestExecuteRescueCreatesDecision(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	var out bytes.Buffer
	err := executeRescue(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{
			"--name", "training-rescue",
			"--event", "Preempted",
			"--workload", "training",
			"--checkpoint", "training-step-100",
			"--target", "cluster-b",
		},
		&out,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "created rescue decision default/training-rescue") {
		t.Fatalf("output = %q", out.String())
	}
	var decision aevrelv1.RescueDecision
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "training-rescue"},
		&decision,
	); err != nil {
		t.Fatal(err)
	}
	if decision.Spec.Event != "Preempted" ||
		decision.Spec.WorkloadRef != "training" ||
		decision.Spec.CheckpointRef != "training-step-100" ||
		decision.Spec.Target != "cluster-b" ||
		decision.Spec.Fallback != "hold" {
		t.Fatalf("decision spec = %+v", decision.Spec)
	}
}
