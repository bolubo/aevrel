package kueueadapter

import (
	"context"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	KueueGroup = "kueue.x-k8s.io"
)

var DefaultWorkloadGVK = schema.GroupVersionKind{
	Group:   KueueGroup,
	Version: "v1beta2",
	Kind:    "Workload",
}

// AddToScheme registers the unstructured Kueue Workload types used by the
// adapter. This avoids taking a compile-time dependency on the Kueue module.
func AddToScheme(scheme *runtime.Scheme, gvk schema.GroupVersionKind) {
	scheme.AddKnownTypeWithName(gvk, &unstructured.Unstructured{})
	listGVK := gvk.GroupVersion().WithKind(gvk.Kind + "List")
	scheme.AddKnownTypeWithName(listGVK, &unstructured.UnstructuredList{})
}

// Reconciler mirrors checkpoint state from an opted-in training Job to the
// Workload labels consumed by Kueue PreemptionConfig. It never creates,
// deletes, suspends, or reprioritizes a workload.
type Reconciler struct {
	client.Client
	Policy          Policy
	WorkloadGVK     schema.GroupVersionKind
	RefreshInterval time.Duration
	Recorder        record.EventRecorder
}

func (r *Reconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(r.workloadGVK())
	if err := r.Get(ctx, request.NamespacedName, workload); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	if workload.GetDeletionTimestamp() != nil {
		return ctrl.Result{}, nil
	}

	enabled, object, source, err := r.observationSource(ctx, workload)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !enabled {
		return ctrl.Result{}, r.clearDecision(ctx, workload)
	}

	observation, parseErr := ParseObservation(
		object.GetAnnotations(),
		object.GetLabels(),
	)
	decision := r.Policy.Decide(observation)
	changed, err := r.applyDecision(
		ctx,
		workload,
		source,
		observation,
		decision,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if parseErr != nil && changed && r.Recorder != nil {
		r.Recorder.Eventf(
			workload,
			corev1.EventTypeWarning,
			"InvalidCheckpointObservation",
			"checkpoint state from %s is invalid: %v",
			source,
			parseErr,
		)
	}

	requeue := r.RefreshInterval
	if requeue <= 0 {
		requeue = 30 * time.Second
	}
	return ctrl.Result{RequeueAfter: requeue}, nil
}

func (r *Reconciler) observationSource(
	ctx context.Context,
	workload *unstructured.Unstructured,
) (bool, metav1.Object, string, error) {
	if IsProtectionEnabled(workload) {
		return true, workload, "workload", nil
	}

	owner := controllerJobOwner(workload.GetOwnerReferences())
	if owner == nil {
		return false, nil, "", nil
	}
	job := &batchv1.Job{}
	key := types.NamespacedName{
		Namespace: workload.GetNamespace(),
		Name:      owner.Name,
	}
	if err := r.Get(ctx, key, job); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil, "", nil
		}
		return false, nil, "", fmt.Errorf("get owner Job %s: %w", key, err)
	}
	if !IsProtectionEnabled(job) {
		return false, nil, "", nil
	}
	return true, job, "job", nil
}

func (r *Reconciler) applyDecision(
	ctx context.Context,
	workload *unstructured.Unstructured,
	source string,
	observation Observation,
	decision Decision,
) (bool, error) {
	base := workload.DeepCopy()
	labels := workload.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	labels[LabelPreemptionProtected] = boolLabel(decision.Protected)
	labels[LabelPreemptionReason] = string(decision.Reason)
	labels[LabelDecisionSource] = source
	if observation.Known && !observation.Invalid {
		labels[LabelProgressPercent] = strconv.Itoa(
			observation.TrainingProgressPercent,
		)
		labels[LabelCheckpointComplete] = boolLabel(
			observation.CheckpointComplete,
		)
	} else {
		delete(labels, LabelProgressPercent)
		delete(labels, LabelCheckpointComplete)
	}
	workload.SetLabels(labels)

	if reflect.DeepEqual(base.GetLabels(), workload.GetLabels()) {
		return false, nil
	}
	if err := r.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return false, fmt.Errorf("patch Workload protection labels: %w", err)
	}
	if r.Recorder == nil {
		return true, nil
	}
	previousReason := ""
	if base.GetLabels() != nil {
		previousReason = base.GetLabels()[LabelPreemptionReason]
	}
	if previousReason != string(decision.Reason) {
		r.Recorder.Eventf(
			workload,
			corev1.EventTypeNormal,
			"PreemptionProtection",
			"protected=%t reason=%s source=%s",
			decision.Protected,
			decision.Reason,
			source,
		)
	}
	return true, nil
}

func (r *Reconciler) clearDecision(
	ctx context.Context,
	workload *unstructured.Unstructured,
) error {
	base := workload.DeepCopy()
	labels := workload.GetLabels()
	if len(labels) == 0 {
		return nil
	}
	delete(labels, LabelPreemptionProtected)
	delete(labels, LabelPreemptionReason)
	delete(labels, LabelProgressPercent)
	delete(labels, LabelCheckpointComplete)
	delete(labels, LabelDecisionSource)
	workload.SetLabels(labels)
	if reflect.DeepEqual(base.GetLabels(), workload.GetLabels()) {
		return nil
	}
	if err := r.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("clear Workload protection labels: %w", err)
	}
	return nil
}

func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(r.workloadGVK())
	return ctrl.NewControllerManagedBy(mgr).
		Named("kueue-preemption-guard").
		For(workload).
		Watches(
			&batchv1.Job{},
			handler.EnqueueRequestsFromMapFunc(r.mapJobToWorkloads),
		).
		Complete(r)
}

func (r *Reconciler) mapJobToWorkloads(
	ctx context.Context,
	object client.Object,
) []reconcile.Request {
	if object.GetNamespace() == "" {
		return nil
	}
	workloads := &unstructured.UnstructuredList{}
	workloads.SetGroupVersionKind(
		r.workloadGVK().GroupVersion().WithKind(r.workloadGVK().Kind + "List"),
	)
	if err := r.List(
		ctx,
		workloads,
		client.InNamespace(object.GetNamespace()),
	); err != nil {
		return nil
	}

	requests := make([]reconcile.Request, 0)
	for i := range workloads.Items {
		workload := &workloads.Items[i]
		for _, owner := range workload.GetOwnerReferences() {
			if owner.UID == object.GetUID() && owner.UID != "" {
				requests = append(requests, reconcile.Request{
					NamespacedName: types.NamespacedName{
						Namespace: workload.GetNamespace(),
						Name:      workload.GetName(),
					},
				})
				break
			}
			if owner.UID == "" &&
				owner.Kind == object.GetObjectKind().GroupVersionKind().Kind &&
				owner.Name == object.GetName() {
				requests = append(requests, reconcile.Request{
					NamespacedName: types.NamespacedName{
						Namespace: workload.GetNamespace(),
						Name:      workload.GetName(),
					},
				})
				break
			}
		}
	}
	return requests
}

func (r *Reconciler) workloadGVK() schema.GroupVersionKind {
	if r.WorkloadGVK.Empty() {
		return DefaultWorkloadGVK
	}
	return r.WorkloadGVK
}

func controllerJobOwner(
	owners []metav1.OwnerReference,
) *metav1.OwnerReference {
	var fallback *metav1.OwnerReference
	for i := range owners {
		owner := &owners[i]
		if owner.Kind != "Job" || !strings.HasPrefix(owner.APIVersion, "batch/") {
			continue
		}
		if fallback == nil {
			fallback = owner
		}
		if owner.Controller != nil && *owner.Controller {
			return owner
		}
	}
	return fallback
}

func boolLabel(value bool) string {
	if value {
		return "1"
	}
	return "0"
}
