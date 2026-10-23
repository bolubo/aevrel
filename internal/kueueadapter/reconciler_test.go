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
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

func TestReconcilerProtectsHighProgressJob(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "90",
		AnnotationCheckpointComplete: "1",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "1")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonProgressThreshold))
	assertLabel(t, got, LabelProgressPercent, "90")
	assertLabel(t, got, LabelCheckpointComplete, "1")
	assertLabel(t, got, LabelDecisionSource, "job")
}

func TestReconcilerProtectsIncompleteCheckpoint(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "20",
		AnnotationCheckpointComplete: "false",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "1")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonCheckpointIncomplete))
	assertLabel(t, got, LabelCheckpointComplete, "0")
}

func TestReconcilerProtectsUnknownState(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection: "enabled",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "1")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonUnknownStateProtected))
	assertMissingLabel(t, got, LabelProgressPercent)
	assertMissingLabel(t, got, LabelCheckpointComplete)
}

func TestReconcilerCanFailOpenUnknownState(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection: "enabled",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	policy := DefaultPolicy()
	policy.ProtectUnknown = false
	r := testReconciler(kubeClient, policy)
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "0")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonUnknownStateEligible))
}

func TestReconcilerProtectsInvalidState(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection:      "enabled",
		AnnotationProgressPercent: "not-a-number",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "1")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonInvalidStateProtected))
	assertMissingLabel(t, got, LabelProgressPercent)
}

func TestReconcilerIgnoresUnrelatedWorkload(t *testing.T) {
	job := testJob(nil)
	workload := testWorkload(job)
	workload.SetLabels(map[string]string{
		LabelPreemptionProtected: "1",
		LabelPreemptionReason:    "stale",
		LabelDecisionSource:      "job",
	})
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertMissingLabel(t, got, LabelPreemptionProtected)
	assertMissingLabel(t, got, LabelPreemptionReason)
	assertMissingLabel(t, got, LabelDecisionSource)
}

func TestReconcilerSupportsWorkloadPublisher(t *testing.T) {
	workload := testWorkload(nil)
	workload.SetAnnotations(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "10",
		AnnotationCheckpointComplete: "1",
	})
	kubeClient := testClient(t, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelPreemptionProtected, "0")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonEligible))
	assertLabel(t, got, LabelDecisionSource, "workload")
}

func TestReconcilerIsIdempotent(t *testing.T) {
	job := testJob(map[string]string{
		AnnotationProtection:         "enabled",
		AnnotationProgressPercent:    "50",
		AnnotationCheckpointComplete: "1",
	})
	workload := testWorkload(job)
	kubeClient := testClient(t, job, workload)

	r := testReconciler(kubeClient, DefaultPolicy())
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	first := getWorkload(t, kubeClient, workload)
	if _, err := r.Reconcile(context.Background(), workloadRequest(workload)); err != nil {
		t.Fatal(err)
	}
	second := getWorkload(t, kubeClient, workload)
	if first.GetResourceVersion() != second.GetResourceVersion() {
		t.Fatalf(
			"idempotent reconcile changed resourceVersion: %s -> %s",
			first.GetResourceVersion(),
			second.GetResourceVersion(),
		)
	}
}

func TestReconcilerUsesInjectedSourceAndProjector(t *testing.T) {
	workload := testWorkload(nil)
	kubeClient := testClient(t, workload)
	projector := &recordingProjector{}
	reconciler := testReconciler(kubeClient, DefaultPolicy())
	reconciler.Source = stubSource{
		context: ObservationContext{
			Enabled: true,
			Source:  "external",
			Object: &metav1.ObjectMeta{
				Annotations: map[string]string{
					AnnotationProgressPercent:    "90",
					AnnotationCheckpointComplete: "1",
				},
			},
		},
	}
	reconciler.Projector = projector

	if _, err := reconciler.Reconcile(
		context.Background(),
		workloadRequest(workload),
	); err != nil {
		t.Fatal(err)
	}
	if !projector.called {
		t.Fatal("injected projector was not called")
	}
	if !projector.decision.Protected ||
		projector.decision.Reason != ReasonProgressThreshold {
		t.Fatalf("projected decision = %#v", projector.decision)
	}
	if projector.source != "external" {
		t.Fatalf("projected source = %q, want external", projector.source)
	}
}

type stubSource struct {
	context ObservationContext
	err     error
}

func (s stubSource) Resolve(
	context.Context,
	*unstructured.Unstructured,
) (ObservationContext, error) {
	return s.context, s.err
}

type recordingProjector struct {
	called   bool
	source   string
	decision Decision
}

func (p *recordingProjector) Project(
	_ context.Context,
	_ *unstructured.Unstructured,
	source string,
	_ Observation,
	decision Decision,
) (bool, error) {
	p.called = true
	p.source = source
	p.decision = decision
	return true, nil
}

func (p *recordingProjector) Clear(
	context.Context,
	*unstructured.Unstructured,
) error {
	return nil
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	AddToScheme(scheme, DefaultWorkloadGVK)
	return scheme
}

func testJob(annotations map[string]string) *batchv1.Job {
	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "batch/v1",
			Kind:       "Job",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:        "train-job",
			Namespace:   "default",
			UID:         types.UID("job-uid"),
			Annotations: annotations,
		},
	}
}

func testWorkload(job *batchv1.Job) *unstructured.Unstructured {
	workload := &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": DefaultWorkloadGVK.GroupVersion().String(),
			"kind":       DefaultWorkloadGVK.Kind,
			"metadata": map[string]any{
				"name":      "job-uid",
				"namespace": "default",
				"uid":       "workload-uid",
			},
		},
	}
	if job != nil {
		controller := true
		workload.SetOwnerReferences([]metav1.OwnerReference{
			{
				APIVersion: "batch/v1",
				Kind:       "Job",
				Name:       job.Name,
				UID:        job.UID,
				Controller: &controller,
			},
		})
	}
	return workload
}

func testClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithObjects(objects...).
		Build()
}

func testReconciler(kubeClient client.Client, policy Policy) *Reconciler {
	return &Reconciler{
		Client:          kubeClient,
		Policy:          policy,
		WorkloadGVK:     DefaultWorkloadGVK,
		RefreshInterval: time.Second,
	}
}

func workloadRequest(workload *unstructured.Unstructured) reconcile.Request {
	return reconcile.Request{
		NamespacedName: types.NamespacedName{
			Namespace: workload.GetNamespace(),
			Name:      workload.GetName(),
		},
	}
}

func getWorkload(
	t *testing.T,
	kubeClient client.Client,
	workload *unstructured.Unstructured,
) *unstructured.Unstructured {
	t.Helper()
	got := &unstructured.Unstructured{}
	got.SetGroupVersionKind(DefaultWorkloadGVK)
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{
			Namespace: workload.GetNamespace(),
			Name:      workload.GetName(),
		},
		got,
	); err != nil {
		t.Fatal(err)
	}
	return got
}

func assertLabel(
	t *testing.T,
	object *unstructured.Unstructured,
	key string,
	want string,
) {
	t.Helper()
	got := object.GetLabels()[key]
	if got != want {
		t.Fatalf("label %s = %q, want %q (labels: %#v)", key, got, want, object.GetLabels())
	}
}

func assertMissingLabel(
	t *testing.T,
	object *unstructured.Unstructured,
	key string,
) {
	t.Helper()
	if value, found := object.GetLabels()[key]; found {
		t.Fatalf("unexpected label %s=%q (labels: %#v)", key, value, object.GetLabels())
	}
}
