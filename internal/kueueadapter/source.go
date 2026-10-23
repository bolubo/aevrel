package kueueadapter

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

// ObservationContext points at the object carrying checkpoint state.
type ObservationContext struct {
	Enabled        bool
	Object         metav1.Object
	Source         string
	Observation    Observation
	HasObservation bool
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

// CheckpointRecordSource resolves the control-plane CheckpointRecord for the
// opted-in Job first, then falls back to publisher annotations when no record
// exists. A stale/incomplete/unverified record is represented as a protected
// observation rather than an eligible one.
type CheckpointRecordSource struct {
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

func (s CheckpointRecordSource) Resolve(
	ctx context.Context,
	workload *unstructured.Unstructured,
) (ObservationContext, error) {
	source, err := (JobObservationSource{Reader: s.Reader}).Resolve(
		ctx,
		workload,
	)
	if err != nil || !source.Enabled {
		return source, err
	}
	annotations, _ := ParseObservation(
		source.Object.GetAnnotations(),
		source.Object.GetLabels(),
	)
	records, err := s.recordsForSource(ctx, workload, source.Object)
	if err != nil {
		return ObservationContext{}, err
	}
	var selected *aevrelv1.CheckpointRecord
	if len(records) > 1 {
		selected, _ = latestCheckpointRecord(records)
	}
	switch {
	case len(records) > 1 && selected == nil:
		source.Source = "checkpoint-record-ambiguous"
		source.Observation = Observation{Invalid: true}
		source.HasObservation = true
		return source, nil
	case len(records) == 0:
		return source, nil
	default:
		if selected == nil {
			selected = &records[0]
		}
		record := *selected
		observation := annotations
		observation.Known = true
		observation.CheckpointComplete = checkpointRecordReady(&record)
		observation.CheckpointVersion = record.Spec.Version
		if record.Status.LatestMtime != "" {
			if latest, parseErr := time.Parse(
				time.RFC3339,
				record.Status.LatestMtime,
			); parseErr == nil {
				age := int64(time.Since(latest).Seconds())
				if age < 0 {
					age = 0
				}
				observation.CheckpointAgeSeconds = &age
			}
		}
		source.Source = "checkpoint-record"
		source.Observation = observation
		source.HasObservation = true
		return source, nil
	}
}

func latestCheckpointRecord(
	records []aevrelv1.CheckpointRecord,
) (*aevrelv1.CheckpointRecord, bool) {
	sort.SliceStable(records, func(i, j int) bool {
		return recordSortKey(&records[i]) > recordSortKey(&records[j])
	})
	if recordSortKey(&records[0]) == recordSortKey(&records[1]) {
		return nil, false
	}
	return &records[0], true
}

func recordSortKey(record *aevrelv1.CheckpointRecord) string {
	step := int64(-1)
	if record.Spec.Step != nil {
		step = *record.Spec.Step
	}
	return fmt.Sprintf(
		"%020d|%s|%s",
		step,
		record.Status.LatestMtime,
		record.CreationTimestamp.UTC().Format(time.RFC3339Nano),
	)
}

func (s CheckpointRecordSource) recordsForSource(
	ctx context.Context,
	workload *unstructured.Unstructured,
	source metav1.Object,
) ([]aevrelv1.CheckpointRecord, error) {
	if s.Reader == nil {
		return nil, fmt.Errorf("checkpoint record reader is nil")
	}
	if name := strings.TrimSpace(
		source.GetAnnotations()[AnnotationCheckpointRecord],
	); name != "" {
		var record aevrelv1.CheckpointRecord
		if err := s.Reader.Get(ctx, client.ObjectKey{
			Namespace: workload.GetNamespace(),
			Name:      name,
		}, &record); err != nil {
			if apierrors.IsNotFound(err) {
				return nil, nil
			}
			return nil, fmt.Errorf("get CheckpointRecord %s: %w", name, err)
		}
		return []aevrelv1.CheckpointRecord{record}, nil
	}
	if name := strings.TrimSpace(
		workload.GetAnnotations()[AnnotationCheckpointRecord],
	); name != "" {
		var record aevrelv1.CheckpointRecord
		if err := s.Reader.Get(ctx, client.ObjectKey{
			Namespace: workload.GetNamespace(),
			Name:      name,
		}, &record); err != nil {
			if apierrors.IsNotFound(err) {
				return nil, nil
			}
			return nil, fmt.Errorf("get CheckpointRecord %s: %w", name, err)
		}
		return []aevrelv1.CheckpointRecord{record}, nil
	}
	owner := controllerJobOwner(workload.GetOwnerReferences())
	if owner == nil {
		return nil, nil
	}
	var records aevrelv1.CheckpointRecordList
	if err := s.Reader.List(
		ctx,
		&records,
		client.InNamespace(workload.GetNamespace()),
	); err != nil {
		return nil, fmt.Errorf("list CheckpointRecords: %w", err)
	}
	matches := make([]aevrelv1.CheckpointRecord, 0)
	for _, record := range records.Items {
		if record.Spec.JobRef == owner.Name || record.Name == owner.Name {
			matches = append(matches, record)
		}
	}
	return matches, nil
}

func checkpointRecordReady(record *aevrelv1.CheckpointRecord) bool {
	readiness := record.Status.Readiness
	return readiness.Status == aevrelv1.ReadinessReady &&
		readiness.Complete &&
		readiness.Fresh &&
		readiness.Loadable != nil &&
		*readiness.Loadable
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
