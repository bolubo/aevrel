package kueueadapter

import (
	"context"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
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
	Source          ObservationSource
	Projector       DecisionProjector
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

	source, err := r.observationSource().Resolve(ctx, workload)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !source.Enabled {
		return ctrl.Result{}, r.projector().Clear(ctx, workload)
	}

	observation := source.Observation
	if !source.HasObservation {
		observation, _ = ParseObservation(
			source.Object.GetAnnotations(),
			source.Object.GetLabels(),
		)
	}
	decision := r.Policy.Decide(observation)
	_, err = r.projector().Project(
		ctx,
		workload,
		source.Source,
		observation,
		decision,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	requeue := r.RefreshInterval
	if requeue <= 0 {
		requeue = 30 * time.Second
	}
	return ctrl.Result{RequeueAfter: requeue}, nil
}

func (r *Reconciler) observationSource() ObservationSource {
	if r.Source != nil {
		return r.Source
	}
	return JobObservationSource{Reader: r.Client}
}

func (r *Reconciler) projector() DecisionProjector {
	if r.Projector != nil {
		return r.Projector
	}
	return WorkloadLabelProjector{Client: r.Client}
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
