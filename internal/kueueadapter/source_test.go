package kueueadapter

import (
	"context"
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
)

func TestCheckpointRecordSourceUsesVerifiedReadiness(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{
		AnnotationProtection: "enabled",
	})
	loadable := true
	record := sourceRecord(job, "step-100")
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessReady,
		Complete:   true,
		Fresh:      true,
		Loadable:   &loadable,
		LastDrill:  "default/training-drill",
		ObservedAt: metav1.Now().UTC().Format(time.RFC3339),
	}
	kubeClient := sourceClient(t, job, workload, record)
	source, err := (CheckpointRecordSource{Reader: kubeClient}).Resolve(
		context.Background(),
		workload,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !source.Enabled || !source.HasObservation ||
		source.Source != "checkpoint-record" {
		t.Fatalf("source = %+v", source)
	}
	if !source.Observation.CheckpointComplete ||
		source.Observation.CheckpointVersion != "step-100" ||
		source.Observation.CheckpointAgeSeconds == nil {
		t.Fatalf("observation = %+v", source.Observation)
	}
	decision := DefaultPolicy().Decide(source.Observation)
	if decision.Protected {
		t.Fatalf("verified record decision = %+v", decision)
	}
}

func TestCheckpointRecordSourceProtectsUnverifiedRecord(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "95",
		AnnotationCheckpointComplete: "1",
	})
	record := sourceRecord(job, "step-99")
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:   aevrelv1.ReadinessUnverified,
		Complete: true,
		Fresh:    true,
	}
	kubeClient := sourceClient(t, job, workload, record)
	source, err := (CheckpointRecordSource{Reader: kubeClient}).Resolve(
		context.Background(),
		workload,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !source.HasObservation ||
		source.Observation.CheckpointComplete {
		t.Fatalf("observation = %+v", source.Observation)
	}
	decision := DefaultPolicy().Decide(source.Observation)
	if !decision.Protected ||
		decision.Reason != ReasonCheckpointIncomplete {
		t.Fatalf("decision = %+v", decision)
	}
}

func TestCheckpointRecordSourceFailsClosedOnAmbiguity(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{
		AnnotationProtection: "enabled",
	})
	first := sourceRecord(job, "step-100")
	second := sourceRecord(job, "step-101")
	second.Name = "training-checkpoint-2"
	kubeClient := sourceClient(t, job, workload, first, second)
	source, err := (CheckpointRecordSource{Reader: kubeClient}).Resolve(
		context.Background(),
		workload,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !source.HasObservation || !source.Observation.Invalid {
		t.Fatalf("source = %+v", source)
	}
	if decision := DefaultPolicy().Decide(source.Observation); !decision.Protected {
		t.Fatalf("ambiguous decision = %+v", decision)
	}
}

func TestCheckpointRecordSourceSelectsLatestStep(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{
		AnnotationProtection: "enabled",
	})
	first := sourceRecord(job, "step-90")
	first.Spec.Step = int64Ptr(90)
	second := sourceRecord(job, "step-100")
	second.Name = "training-checkpoint-2"
	second.Spec.Step = int64Ptr(100)
	kubeClient := sourceClient(t, job, workload, first, second)
	source, err := (CheckpointRecordSource{Reader: kubeClient}).Resolve(
		context.Background(),
		workload,
	)
	if err != nil {
		t.Fatal(err)
	}
	if source.Observation.CheckpointVersion != "step-100" {
		t.Fatalf("observation = %+v", source.Observation)
	}
}

func TestCheckpointRecordSourceFallsBackToAnnotations(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "20",
		AnnotationCheckpointComplete: "1",
	})
	kubeClient := sourceClient(t, job, workload)
	source, err := (CheckpointRecordSource{Reader: kubeClient}).Resolve(
		context.Background(),
		workload,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !source.Enabled || source.HasObservation {
		t.Fatalf("source = %+v", source)
	}
	observation, err := ParseObservation(
		source.Object.GetAnnotations(),
		source.Object.GetLabels(),
	)
	if err != nil {
		t.Fatal(err)
	}
	if !DefaultPolicy().Decide(observation).Eligible {
		t.Fatalf("annotation fallback decision = %+v", observation)
	}
}

func TestReconcilerUsesCheckpointRecordDecision(t *testing.T) {
	job, _ := sourceWorkload(map[string]string{
		AnnotationProtection: "enabled",
	})
	workload := testWorkload(job)
	loadable := true
	record := sourceRecord(job, "step-100")
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessReady,
		Complete:   true,
		Fresh:      true,
		Loadable:   &loadable,
		LastDrill:  "default/training-drill",
		ObservedAt: metav1.Now().UTC().Format(time.RFC3339),
	}
	kubeClient := sourceClient(t, job, workload, record)
	reconciler := testReconciler(kubeClient, DefaultPolicy())
	reconciler.Source = CheckpointRecordSource{Reader: kubeClient}
	if _, err := reconciler.Reconcile(
		context.Background(),
		workloadRequest(workload),
	); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "0")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonEligible))
	assertLabel(t, got, LabelDecisionSource, "checkpoint-record")
	assertLabel(t, got, LabelCheckpointComplete, "1")
}

func sourceWorkload(
	annotations map[string]string,
) (*batchv1.Job, *unstructured.Unstructured) {
	job := &batchv1.Job{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "batch/v1",
			Kind:       "Job",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:        "training",
			Namespace:   "default",
			UID:         types.UID("job-uid"),
			Annotations: annotations,
		},
	}
	workload := &unstructured.Unstructured{}
	workload.SetAPIVersion(DefaultWorkloadGVK.GroupVersion().String())
	workload.SetKind(DefaultWorkloadGVK.Kind)
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
	return job, workload
}

func sourceClient(
	t *testing.T,
	objects ...runtime.Object,
) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	AddToScheme(scheme, DefaultWorkloadGVK)
	return fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objects...).
		Build()
}

func sourceRecord(
	job *batchv1.Job,
	version string,
) *aevrelv1.CheckpointRecord {
	return &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-checkpoint",
			Namespace: job.Namespace,
		},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          job.Name,
			Version:         version,
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/" + version,
			SourceWorldSize: 4,
		},
		Status: aevrelv1.CheckpointRecordStatus{
			LatestMtime: time.Now().Add(-5 * time.Second).UTC().Format(
				time.RFC3339,
			),
		},
	}
}
