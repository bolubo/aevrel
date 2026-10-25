// Package integration exercises the Kueue adapter through its public surface
// only. The tests here answer the question a reviewer actually asks: does the
// published envelope agree with the decision the adapter made?
package integration

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
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/kueueadapter"
)

type scenario struct {
	name            string
	annotations     map[string]string
	readiness       *aevrelv1.CheckpointReadinessStatus
	wantProtected   bool
	wantEnvelope    bool
	wantRestoreMode string
	wantCost        string
}

func scenarios() []scenario {
	ready := &aevrelv1.CheckpointReadinessStatus{}
	ready.Status = aevrelv1.ReadinessReady
	ready.Complete = true
	ready.Fresh = true
	ready.Loadable = boolPtr(true)
	ready.LastDrill = "default/training-drill"
	ready.LastDrillVersion = "step-100"
	ready.LastDrillDigest = "sha256:abc"
	ready.ObservedAt = time.Now().UTC().Format(time.RFC3339)

	unverified := &aevrelv1.CheckpointReadinessStatus{}
	unverified.Status = aevrelv1.ReadinessUnverified
	unverified.Complete = true
	unverified.Fresh = true
	unverified.ObservedAt = time.Now().UTC().Format(time.RFC3339)

	stale := &aevrelv1.CheckpointReadinessStatus{}
	stale.Status = aevrelv1.ReadinessStale
	stale.Complete = true
	stale.Fresh = false
	stale.Loadable = boolPtr(true)
	stale.LastDrill = "default/training-drill"
	stale.ObservedAt = time.Now().UTC().Format(time.RFC3339)

	return []scenario{
		{
			name:            "ready record publishes a restoring envelope",
			annotations:     map[string]string{kueueadapter.AnnotationProtection: "enabled"},
			readiness:       ready,
			wantProtected:   false,
			wantEnvelope:    true,
			wantRestoreMode: "restore",
			wantCost:        "resume",
		},
		{
			// Fresh without a loadability verdict cannot be published, so the
			// binary protection is the only signal. Both are conservative.
			name:          "unverified record is protected and publishes no envelope",
			annotations:   map[string]string{kueueadapter.AnnotationProtection: "enabled"},
			readiness:     unverified,
			wantProtected: true,
			wantEnvelope:  false,
		},
		{
			name:            "stale record is protected with a hold envelope",
			annotations:     map[string]string{kueueadapter.AnnotationProtection: "enabled"},
			readiness:       stale,
			wantProtected:   true,
			wantEnvelope:    true,
			wantRestoreMode: "hold",
			wantCost:        "unknown",
		},
		{
			name: "no record falls back to annotations without claiming facts",
			annotations: map[string]string{
				kueueadapter.AnnotationProtection:         "enabled",
				kueueadapter.AnnotationProgressPercent:    "20",
				kueueadapter.AnnotationCheckpointComplete: "1",
			},
			wantProtected:   false,
			wantEnvelope:    true,
			wantRestoreMode: "hold",
			wantCost:        "unknown",
		},
	}
}

func TestKueueContractAgreesWithTheDecisionItExplains(t *testing.T) {
	for _, testCase := range scenarios() {
		t.Run(testCase.name, func(t *testing.T) {
			kubeClient, workload := fixture(t, testCase.annotations, testCase.readiness)

			source, err := (kueueadapter.CheckpointRecordSource{
				Reader: kubeClient,
			}).Resolve(context.Background(), workload)
			if err != nil {
				t.Fatal(err)
			}
			observation := source.Observation
			if !source.HasObservation {
				parsed, parseErr := kueueadapter.ParseObservation(
					source.Object.GetAnnotations(),
					source.Object.GetLabels(),
				)
				if parseErr != nil {
					t.Fatal(parseErr)
				}
				observation = parsed
			}
			wantDecision := kueueadapter.DefaultPolicy().Decide(observation)

			reconciler := &kueueadapter.Reconciler{
				Client:          kubeClient,
				Policy:          kueueadapter.DefaultPolicy(),
				WorkloadGVK:     kueueadapter.DefaultWorkloadGVK,
				RefreshInterval: time.Second,
				Source:          kueueadapter.CheckpointRecordSource{Reader: kubeClient},
				Projector: kueueadapter.WorkloadLabelProjector{
					Client:         kubeClient,
					EnvelopeLabels: true,
				},
			}
			if _, err := reconciler.Reconcile(
				context.Background(),
				ctrl.Request{NamespacedName: types.NamespacedName{
					Namespace: workload.GetNamespace(),
					Name:      workload.GetName(),
				}},
			); err != nil {
				t.Fatal(err)
			}

			got := loadWorkload(t, kubeClient, workload)
			labels := got.GetLabels()

			// 1. The decision the adapter published is the decision the policy
			// makes from the same source.
			wantProtectedLabel := "0"
			if wantDecision.Protected {
				wantProtectedLabel = "1"
			}
			if labels[kueueadapter.LabelPreemptionProtected] != wantProtectedLabel {
				t.Fatalf(
					"protected label = %q, policy says %+v",
					labels[kueueadapter.LabelPreemptionProtected],
					wantDecision,
				)
			}
			if labels[kueueadapter.LabelPreemptionReason] != string(wantDecision.Reason) {
				t.Fatalf(
					"reason label = %q, policy says %q",
					labels[kueueadapter.LabelPreemptionReason],
					wantDecision.Reason,
				)
			}
			if wantDecision.Protected != testCase.wantProtected {
				t.Fatalf(
					"fixture sanity: policy protected = %t, want %t",
					wantDecision.Protected,
					testCase.wantProtected,
				)
			}

			// 2. The envelope is present exactly when the fact set passed
			// conformance, and it never contradicts the decision.
			envelopePresent := labels[kueueadapter.LabelEnvelopeSchemaVersion] != ""
			if envelopePresent != testCase.wantEnvelope {
				t.Fatalf(
					"envelope present = %t, want %t (violations: %v)",
					envelopePresent,
					testCase.wantEnvelope,
					source.Facts.Conformance.Violations,
				)
			}
			if envelopePresent != source.Facts.Conformance.Valid {
				t.Fatal("an envelope is published iff the fact set is valid")
			}
			if !testCase.wantEnvelope {
				return
			}

			// 3. restoreMode and the consumer-facing cost tell one story.
			if labels[kueueadapter.LabelEnvelopeRestoreMode] != testCase.wantRestoreMode {
				t.Fatalf(
					"restore-mode label = %q, want %q",
					labels[kueueadapter.LabelEnvelopeRestoreMode],
					testCase.wantRestoreMode,
				)
			}
			view := kueueadapter.KueueTranslator{}.Translate(source.Facts)
			if cost := fieldValue(view, "preemptionCost"); cost != testCase.wantCost {
				t.Fatalf("preemptionCost = %q, want %q", cost, testCase.wantCost)
			}
			if source.Facts.RestoreMode != testCase.wantRestoreMode {
				t.Fatalf(
					"facts.RestoreMode = %q, want %q",
					source.Facts.RestoreMode,
					testCase.wantRestoreMode,
				)
			}
		})
	}
}

func fixture(
	t *testing.T,
	annotations map[string]string,
	readiness *aevrelv1.CheckpointReadinessStatus,
) (client.Client, *unstructured.Unstructured) {
	t.Helper()
	job := &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        "training",
			Namespace:   "default",
			UID:         types.UID("job-uid"),
			Annotations: annotations,
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

	objects := []runtime.Object{job, workload}
	if readiness != nil {
		objects = append(objects, &aevrelv1.CheckpointRecord{
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
				Readiness:   *readiness,
			},
		})
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kueueadapter.AddToScheme(scheme, kueueadapter.DefaultWorkloadGVK)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objects...).
		Build()
	return kubeClient, workload
}

func loadWorkload(
	t *testing.T,
	kubeClient client.Client,
	workload *unstructured.Unstructured,
) *unstructured.Unstructured {
	t.Helper()
	got := &unstructured.Unstructured{}
	got.SetGroupVersionKind(kueueadapter.DefaultWorkloadGVK)
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKey{
			Namespace: workload.GetNamespace(),
			Name:      workload.GetName(),
		},
		got,
	); err != nil {
		t.Fatal(err)
	}
	return got
}

func fieldValue(view kueueadapter.ConsumerView, name string) string {
	for _, field := range view.Fields {
		if field.Name == name {
			return field.Value
		}
	}
	return ""
}

func boolPtr(value bool) *bool {
	return &value
}
