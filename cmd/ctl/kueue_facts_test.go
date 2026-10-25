package main

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/kueueadapter"
)

func explainFactsClient(t *testing.T) client.Client {
	t.Helper()
	job := &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training",
			Namespace: "default",
			UID:       types.UID("job-uid"),
			Annotations: map[string]string{
				kueueadapter.AnnotationProtection: "enabled",
			},
		},
	}
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(kueueadapter.DefaultWorkloadGVK)
	workload.SetName("training-workload")
	workload.SetNamespace("default")
	controller := true
	workload.SetOwnerReferences([]metav1.OwnerReference{{
		APIVersion: "batch/v1",
		Kind:       "Job",
		Name:       job.Name,
		UID:        job.UID,
		Controller: &controller,
	}})

	loadable := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-checkpoint",
			Namespace: "default",
		},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:  job.Name,
			Version: "step-100",
		},
		Status: aevrelv1.CheckpointRecordStatus{
			LatestMtime: time.Now().UTC().Format(time.RFC3339),
			Readiness: aevrelv1.CheckpointReadinessStatus{
				Status:              aevrelv1.ReadinessReady,
				Complete:            true,
				Fresh:               true,
				Loadable:            &loadable,
				LastDrill:           "default/training-drill",
				LastDrillVersion:    "step-100",
				LastDrillDigest:     "sha256:abc",
				ObservedAt:          time.Now().UTC().Format(time.RFC3339),
				EstimatedRTOSeconds: factInt64Ptr(45),
			},
		},
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kueueadapter.AddToScheme(scheme, kueueadapter.DefaultWorkloadGVK)
	return fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(job, workload, record).
		Build()
}

func factInt64Ptr(value int64) *int64 {
	return &value
}

func TestKueueExplainReportsAuthoritativeFacts(t *testing.T) {
	kubeClient := explainFactsClient(t)
	var out bytes.Buffer
	if err := executeKueue(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"explain", "training-workload"},
		&out,
	); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	for _, wanted := range []string{
		"Checkpoint facts:",
		"Schema:        aevrel.dev/v1",
		"Source:        checkpoint-record",
		"Complete:      true",
		"Fresh:         true",
		"Loadable:      true",
		"Movable:       unknown",
		"RestoreMode:   restore",
		"EstimatedRTO:  45",
		"Selected:      training-checkpoint (1 candidate records)",
		"Conformance:   valid",
	} {
		if !strings.Contains(text, wanted) {
			t.Fatalf("explain missing %q:\n%s", wanted, text)
		}
	}
}

func TestKueueExplainJSONIsAStableFactsContract(t *testing.T) {
	kubeClient := explainFactsClient(t)
	var out bytes.Buffer
	if err := executeKueue(
		context.Background(),
		kubeClient,
		"default",
		true,
		[]string{"explain", "training-workload"},
		&out,
	); err != nil {
		t.Fatal(err)
	}
	var decoded struct {
		SchemaVersion     string         `json:"schemaVersion"`
		ProtectionEnabled bool           `json:"protectionEnabled"`
		CheckpointFacts   map[string]any `json:"checkpointFacts"`
	}
	if err := json.Unmarshal(out.Bytes(), &decoded); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, out.String())
	}
	if decoded.SchemaVersion != kueueadapter.SchemaVersion {
		t.Fatalf("schemaVersion = %q", decoded.SchemaVersion)
	}
	if !decoded.ProtectionEnabled {
		t.Fatal("protectionEnabled must reflect the resolved source")
	}
	if decoded.CheckpointFacts == nil {
		t.Fatalf("checkpointFacts missing:\n%s", out.String())
	}
	if decoded.CheckpointFacts["schemaVersion"] != kueueadapter.SchemaVersion {
		t.Fatalf("fact schemaVersion = %v", decoded.CheckpointFacts["schemaVersion"])
	}
	if decoded.CheckpointFacts["loadable"] != true {
		t.Fatalf("loadable = %v", decoded.CheckpointFacts["loadable"])
	}
	if decoded.CheckpointFacts["restoreMode"] != "restore" {
		t.Fatalf("restoreMode = %v", decoded.CheckpointFacts["restoreMode"])
	}
	if decoded.CheckpointFacts["candidates"] != float64(1) {
		t.Fatalf("candidates = %v", decoded.CheckpointFacts["candidates"])
	}
}

func TestKueueExplainSaysWhenProtectionIsOff(t *testing.T) {
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(kueueadapter.DefaultWorkloadGVK)
	workload.SetName("plain-workload")
	workload.SetNamespace("default")
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
		[]string{"explain", "plain-workload"},
		&out,
	); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(
		out.String(),
		"preemption protection is not enabled",
	) {
		t.Fatalf("explain = %s", out.String())
	}
}
