package kueueadapter

import (
	"context"
	"fmt"
	"reflect"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// DecisionProjector is the adapter's output boundary. Kueue only understands
// labels, so this implementation keeps the projection deliberately boring.
type DecisionProjector interface {
	Project(
		ctx context.Context,
		workload *unstructured.Unstructured,
		source string,
		observation Observation,
		decision Decision,
	) (bool, error)
	Clear(
		ctx context.Context,
		workload *unstructured.Unstructured,
	) error
}

// WorkloadLabelProjector writes checkpoint state into Workload labels consumed
// by Kueue PreemptionConfig.numericLabels.
type WorkloadLabelProjector struct {
	Client   client.Client
	Recorder record.EventRecorder
}

func (p WorkloadLabelProjector) Project(
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
	if err := p.Client.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return false, fmt.Errorf("patch Workload protection labels: %w", err)
	}
	if p.Recorder == nil {
		return true, nil
	}
	previousReason := ""
	if base.GetLabels() != nil {
		previousReason = base.GetLabels()[LabelPreemptionReason]
	}
	if previousReason != string(decision.Reason) {
		p.Recorder.Eventf(
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

func (p WorkloadLabelProjector) Clear(
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
	if err := p.Client.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("clear Workload protection labels: %w", err)
	}
	return nil
}

func boolLabel(value bool) string {
	if value {
		return "1"
	}
	return "0"
}
