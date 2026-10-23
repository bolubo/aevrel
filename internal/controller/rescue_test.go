package controller

import (
	"context"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

func TestRescueDecisionReconcilerMigratesVerifiedCheckpoint(t *testing.T) {
	loadable := true
	movable := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Status: aevrelv1.CheckpointRecordStatus{
			Readiness: aevrelv1.CheckpointReadinessStatus{
				Status:     aevrelv1.ReadinessReady,
				Complete:   true,
				Fresh:      true,
				Loadable:   &loadable,
				Movable:    &movable,
				LastDrill:  "default/drill",
				ObservedAt: time.Now().UTC().Format(time.RFC3339),
			},
		},
	}
	decision := &aevrelv1.RescueDecision{
		ObjectMeta: metav1.ObjectMeta{Name: "rescue", Namespace: "default"},
		Spec: aevrelv1.RescueDecisionSpec{
			Event:         "Preempted",
			CheckpointRef: "job42",
			Target:        "cluster-b",
			Fallback:      "hold",
		},
	}
	scheme := rescueScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.RescueDecision{}).
		WithObjects(record, decision).
		Build()
	reconciler := &RescueDecisionReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "rescue",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.RescueDecision
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "rescue"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Action != string(control.RescueMigrate) ||
		got.Status.ReasonCode != control.RescueReasonMigrateReady ||
		got.Status.Target != "cluster-b" ||
		got.Status.ProofRef != "default/drill" {
		t.Fatalf("decision status = %+v", got.Status)
	}
}

func TestRescueEventReconcilerCreatesOptedInDecision(t *testing.T) {
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training",
			Namespace: "default",
			Labels: map[string]string{
				"aevrel.dev/rescue": "enabled",
			},
			Annotations: map[string]string{
				"aevrel.dev/checkpoint-record": "training-checkpoint",
			},
		},
	}
	event := &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-preempted",
			Namespace: "default",
			UID:       types.UID("event-uid"),
		},
		InvolvedObject: corev1.ObjectReference{
			APIVersion: "batch/v1",
			Kind:       "Job",
			Namespace:  "default",
			Name:       "training",
			UID:        types.UID("job-uid"),
		},
		Reason: "Preempted",
		Type:   corev1.EventTypeWarning,
	}
	scheme := rescueScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(job, event).
		Build()
	reconciler := &RescueEventReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "training-preempted",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var decisions aevrelv1.RescueDecisionList
	if err := kubeClient.List(context.Background(), &decisions); err != nil {
		t.Fatal(err)
	}
	if len(decisions.Items) != 1 {
		t.Fatalf("created decisions = %d, want 1", len(decisions.Items))
	}
	got := decisions.Items[0]
	if got.Spec.Event != "Preempted" ||
		got.Spec.WorkloadRef != "training" ||
		got.Spec.CheckpointRef != "training-checkpoint" ||
		got.Spec.Fallback != "hold" {
		t.Fatalf("decision spec = %+v", got.Spec)
	}
}

func TestRescueEventReconcilerIgnoresUnoptedWorkload(t *testing.T) {
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "other",
			Namespace: "default",
		},
	}
	event := &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "other-preempted",
			Namespace: "default",
			UID:       types.UID("event-uid"),
		},
		InvolvedObject: corev1.ObjectReference{
			APIVersion: "batch/v1",
			Kind:       "Job",
			Namespace:  "default",
			Name:       "other",
			UID:        types.UID("job-uid"),
		},
		Reason: "Preempted",
		Type:   corev1.EventTypeWarning,
	}
	scheme := rescueScheme(t)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(job, event).
		Build()
	reconciler := &RescueEventReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "other-preempted",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var decisions aevrelv1.RescueDecisionList
	if err := kubeClient.List(
		context.Background(),
		&decisions,
		client.InNamespace("default"),
	); err != nil {
		t.Fatal(err)
	}
	if len(decisions.Items) != 0 {
		t.Fatalf("created decisions = %d, want 0", len(decisions.Items))
	}
}

func rescueScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := batchv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return scheme
}
