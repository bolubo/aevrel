package kueueadapter

import (
	"context"
	"fmt"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ObservationContext points at the object carrying checkpoint state.
type ObservationContext struct {
	Enabled bool
	Object  metav1.Object
	Source  string
}

// ObservationSource is the adapter's input boundary. Today checkpoint state
// comes from a Job annotation or, for a headless setup, the Workload itself.
// A sidecar or external state provider can replace this without changing the
// policy or projector.
type ObservationSource interface {
	Resolve(
		ctx context.Context,
		workload *unstructured.Unstructured,
	) (ObservationContext, error)
}

// JobObservationSource resolves a Kueue Workload to its owning batch Job.
type JobObservationSource struct {
	Reader client.Reader
}

func (s JobObservationSource) Resolve(
	ctx context.Context,
	workload *unstructured.Unstructured,
) (ObservationContext, error) {
	if IsProtectionEnabled(workload) {
		return ObservationContext{
			Enabled: true,
			Object:  workload,
			Source:  "workload",
		}, nil
	}

	owner := controllerJobOwner(workload.GetOwnerReferences())
	if owner == nil {
		return ObservationContext{}, nil
	}
	job := &batchv1.Job{}
	key := types.NamespacedName{
		Namespace: workload.GetNamespace(),
		Name:      owner.Name,
	}
	if err := s.Reader.Get(ctx, key, job); err != nil {
		if apierrors.IsNotFound(err) {
			return ObservationContext{}, nil
		}
		return ObservationContext{}, fmt.Errorf("get owner Job %s: %w", key, err)
	}
	if !IsProtectionEnabled(job) {
		return ObservationContext{}, nil
	}
	return ObservationContext{
		Enabled: true,
		Object:  job,
		Source:  "job",
	}, nil
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
