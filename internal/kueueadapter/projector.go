package kueueadapter

import (
	"context"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"time"

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

// FactProjector is an optional extension of DecisionProjector: a projector that
// can also publish the scheduler envelope. It is a separate interface on
// purpose. The envelope is transport, not decision input, and a projector that
// only implements DecisionProjector keeps working unchanged.
type FactProjector interface {
	ProjectFacts(
		ctx context.Context,
		workload *unstructured.Unstructured,
		facts CheckpointFacts,
	) (bool, error)
	ClearFacts(
		ctx context.Context,
		workload *unstructured.Unstructured,
	) error
}

// WorkloadLabelProjector writes checkpoint state into Workload labels consumed
// by Kueue PreemptionConfig.numericLabels.
//
// EnvelopeLabels additionally publishes the scheduler/runtime contract facts.
// It defaults to false: enabling it changes which labels exist, never which
// decision is made.
type WorkloadLabelProjector struct {
	Client         client.Client
	Recorder       record.EventRecorder
	EnvelopeLabels bool
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

// ProjectFacts publishes the scheduler envelope. A fact set that failed
// conformance is never published: the previous envelope is withdrawn instead,
// so a consumer can be sure that a present envelope was conformance-checked and
// an absent one means "no usable facts", not "the old facts still hold".
func (p WorkloadLabelProjector) ProjectFacts(
	ctx context.Context,
	workload *unstructured.Unstructured,
	facts CheckpointFacts,
) (bool, error) {
	if !p.EnvelopeLabels {
		return false, nil
	}
	desired := envelopeLabelValues(facts)
	base := workload.DeepCopy()
	labels := workload.GetLabels()
	if labels == nil {
		labels = map[string]string{}
	}
	for _, key := range EnvelopeLabels {
		delete(labels, key)
	}
	for key, value := range desired {
		labels[key] = value
	}
	workload.SetLabels(labels)
	if reflect.DeepEqual(base.GetLabels(), workload.GetLabels()) {
		return false, nil
	}
	if err := p.Client.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return false, fmt.Errorf("patch Workload envelope labels: %w", err)
	}
	return true, nil
}

// ClearFacts removes every envelope label. It is safe to call when no envelope
// was ever written.
func (p WorkloadLabelProjector) ClearFacts(
	ctx context.Context,
	workload *unstructured.Unstructured,
) error {
	if !p.EnvelopeLabels {
		return nil
	}
	base := workload.DeepCopy()
	labels := workload.GetLabels()
	if len(labels) == 0 {
		return nil
	}
	for _, key := range EnvelopeLabels {
		delete(labels, key)
	}
	workload.SetLabels(labels)
	if reflect.DeepEqual(base.GetLabels(), workload.GetLabels()) {
		return nil
	}
	if err := p.Client.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("clear Workload envelope labels: %w", err)
	}
	return nil
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
	for _, key := range EnvelopeLabels {
		delete(labels, key)
	}
	workload.SetLabels(labels)
	if reflect.DeepEqual(base.GetLabels(), workload.GetLabels()) {
		return nil
	}
	if err := p.Client.Patch(ctx, workload, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("clear Workload protection labels: %w", err)
	}
	return nil
}

// envelopeLabelValues renders the labels for one fact set, or nil when the fact
// set is not publishable. Everything it writes is derived from Facts, so the
// envelope can always be recomputed from the authoritative record.
func envelopeLabelValues(facts CheckpointFacts) map[string]string {
	if !facts.Conformance.Valid {
		return nil
	}
	values := map[string]string{
		LabelEnvelopeSchemaVersion: SchemaLabelValue,
		LabelEnvelopeComplete:      factBoolLabel(facts.Complete),
		LabelEnvelopeFresh:         factBoolLabel(facts.Fresh),
		LabelEnvelopeLoadable:      factBoolLabel(facts.Loadable),
		LabelEnvelopeMovable:       factBoolLabel(facts.Movable),
		LabelEnvelopeSource:        facts.Source,
		LabelEnvelopeRestoreMode:   facts.RestoreMode,
		LabelEnvelopeEstimatedRTO:  factRTOLabel(facts.EstimatedRTOSeconds),
		LabelEnvelopeConformance:   "valid",
	}
	if codes := reasonCodesLabel(facts.ReasonCodes); codes != "" {
		values[LabelEnvelopeReasonCodes] = codes
	}
	if observedAt := factTimeLabel(facts.ObservedAt); observedAt != "" {
		values[LabelEnvelopeObservedAt] = observedAt
	}
	return values
}

// maxLabelValueLength is the Kubernetes limit for one label value. A fact that
// does not fit inside it is published as "unknown" instead of as a truncated
// string: a truncated code list would read as the complete list.
const maxLabelValueLength = 63

// reasonCodesLabel renders the readiness reasons for the label transport. The
// authoritative list always stays on the CheckpointRecord; when it cannot be
// carried verbatim the label says "unknown" so a consumer knows to read the
// record rather than trust a partial list.
func reasonCodesLabel(codes []string) string {
	joined := strings.Join(codes, "_")
	if joined == "" {
		return ""
	}
	if len(joined) > maxLabelValueLength {
		return FactValueUnknown
	}
	return joined
}

func factBoolLabel(value *bool) string {
	if value == nil {
		return FactValueUnknown
	}
	return boolLabel(*value)
}

func factRTOLabel(value *int64) string {
	if value == nil {
		return FactValueUnknown
	}
	return strconv.FormatInt(*value, 10)
}

func factTimeLabel(observedAt string) string {
	if observedAt == "" {
		return ""
	}
	parsed, err := time.Parse(time.RFC3339, observedAt)
	if err != nil {
		return ""
	}
	return parsed.UTC().Format(EnvelopeTimeLayout)
}

func boolLabel(value bool) string {
	if value {
		return "1"
	}
	return "0"
}
