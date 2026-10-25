package main

import (
	"context"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/bolubo/aevrel/internal/kueueadapter"
)

func executeKueue(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	if kubeClient == nil {
		return fmt.Errorf("Kubernetes client is not configured")
	}
	action := "status"
	if len(args) > 0 {
		action = args[0]
	}
	switch action {
	case "status":
		if len(args) > 1 {
			return fmt.Errorf("usage: aevrelctl kueue status")
		}
		return executeKueueStatus(ctx, kubeClient, namespace, jsonOut, out)
	case "explain":
		if len(args) != 2 {
			return fmt.Errorf("usage: aevrelctl kueue explain NAME")
		}
		return executeKueueExplain(
			ctx,
			kubeClient,
			namespace,
			args[1],
			jsonOut,
			out,
		)
	default:
		return fmt.Errorf("unknown kueue command: %s", action)
	}
}

func executeKueueStatus(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	out io.Writer,
) error {
	workloads := &unstructured.UnstructuredList{}
	workloads.SetGroupVersionKind(
		kueueadapter.DefaultWorkloadGVK.GroupVersion().WithKind("WorkloadList"),
	)
	if err := kubeClient.List(
		ctx,
		workloads,
		client.InNamespace(namespace),
	); err != nil {
		return err
	}
	sort.Slice(workloads.Items, func(i, j int) bool {
		return workloads.Items[i].GetName() < workloads.Items[j].GetName()
	})
	if jsonOut {
		return writeJSON(out, workloads.Items)
	}
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(
		writer,
		"NAME\tPROTECTED\tREASON\tPROGRESS\tCHECKPOINT\tSOURCE\tSTATE",
	)
	for index := range workloads.Items {
		workload := &workloads.Items[index]
		fmt.Fprintf(
			writer,
			"%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			workload.GetName(),
			labelValue(workload, kueueadapter.LabelPreemptionProtected),
			labelValue(workload, kueueadapter.LabelPreemptionReason),
			labelValue(workload, kueueadapter.LabelProgressPercent),
			labelValue(workload, kueueadapter.LabelCheckpointComplete),
			labelValue(workload, kueueadapter.LabelDecisionSource),
			kueueWorkloadState(workload),
		)
	}
	return nil
}

// executeKueueExplain reports why a decision was reached, not just what it was.
// The facts block comes from the same source the adapter acts on, so the
// explanation cannot drift from the decision.
func executeKueueExplain(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
	jsonOut bool,
	out io.Writer,
) error {
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(kueueadapter.DefaultWorkloadGVK)
	if err := kubeClient.Get(ctx, client.ObjectKey{
		Namespace: namespace,
		Name:      name,
	}, workload); err != nil {
		return err
	}

	source, factsErr := (kueueadapter.CheckpointRecordSource{
		Reader: kubeClient,
	}).Resolve(ctx, workload)
	factsAvailable := factsErr == nil && source.HasFacts

	if jsonOut {
		output := kueueExplainOutput{
			SchemaVersion:     kueueadapter.SchemaVersion,
			Workload:          workload,
			ProtectionEnabled: source.Enabled,
			FactsError:        errToString(factsErr),
		}
		if factsAvailable {
			output.CheckpointFacts = &source.Facts
		}
		return writeJSON(out, output)
	}

	fmt.Fprintf(out, "Name:         %s\n", name)
	fmt.Fprintf(out, "Namespace:    %s\n", namespace)
	fmt.Fprintf(
		out,
		"Protected:    %s\n",
		emptyDash(labelValue(workload, kueueadapter.LabelPreemptionProtected)),
	)
	fmt.Fprintf(
		out,
		"Reason:       %s\n",
		emptyDash(labelValue(workload, kueueadapter.LabelPreemptionReason)),
	)
	fmt.Fprintf(
		out,
		"Progress:     %s\n",
		emptyDash(labelValue(workload, kueueadapter.LabelProgressPercent)),
	)
	fmt.Fprintf(
		out,
		"Checkpoint:   %s\n",
		emptyDash(labelValue(workload, kueueadapter.LabelCheckpointComplete)),
	)
	fmt.Fprintf(
		out,
		"Source:       %s\n",
		emptyDash(labelValue(workload, kueueadapter.LabelDecisionSource)),
	)
	fmt.Fprintf(out, "State:        %s\n", kueueWorkloadState(workload))
	for _, condition := range kueueConditions(workload) {
		fmt.Fprintf(
			out,
			"Condition:    %s=%s %s\n",
			condition.Type,
			condition.Status,
			condition.Reason,
		)
	}
	if protected := labelValue(
		workload,
		kueueadapter.LabelPreemptionProtected,
	); protected == "1" {
		fmt.Fprintln(
			out,
			"Next: inspect the checkpoint record/drill; protected workloads are excluded only when the strict profile is configured",
		)
	} else if protected == "0" {
		fmt.Fprintln(
			out,
			"Next: workload is eligible for configurable preemption; verify Kueue events and ClusterQueue settings",
		)
	} else {
		fmt.Fprintln(
			out,
			"Next: adapter has not published a decision yet; check adapter logs and the Workload owner Job",
		)
	}
	writeFactsText(out, source, factsErr)
	return nil
}

// writeFactsText prints the read-only fact set. A failure to read it is
// reported as a fact of its own: the command still explains the decision
// labels, and never panics on a missing cluster object.
func writeFactsText(
	out io.Writer,
	source kueueadapter.ObservationContext,
	factsErr error,
) {
	fmt.Fprintln(out, "Checkpoint facts:")
	if factsErr != nil {
		fmt.Fprintf(out, "  (unavailable: %s)\n", factsErr)
		return
	}
	if !source.Enabled {
		fmt.Fprintln(
			out,
			"  (preemption protection is not enabled on this workload or its owner Job)",
		)
		return
	}
	if !source.HasFacts {
		fmt.Fprintln(out, "  (no checkpoint facts are available)")
		return
	}
	facts := source.Facts
	fmt.Fprintf(out, "  Schema:        %s\n", facts.SchemaVersion)
	fmt.Fprintf(out, "  Source:        %s\n", facts.Source)
	fmt.Fprintf(out, "  Status:        %s\n", emptyDash(facts.Status))
	fmt.Fprintf(out, "  Complete:      %s\n", factBoolText(facts.Complete))
	fmt.Fprintf(out, "  Fresh:         %s\n", factBoolText(facts.Fresh))
	fmt.Fprintf(out, "  Loadable:      %s\n", factBoolText(facts.Loadable))
	fmt.Fprintf(out, "  Movable:       %s\n", factBoolText(facts.Movable))
	fmt.Fprintf(out, "  ObservedAt:    %s\n", emptyDash(facts.ObservedAt))
	fmt.Fprintf(out, "  RestoreMode:   %s\n", emptyDash(facts.RestoreMode))
	if facts.RestoreReasonCode != "" {
		fmt.Fprintf(out, "  RestoreReason: %s\n", facts.RestoreReasonCode)
	}
	fmt.Fprintf(
		out,
		"  EstimatedRTO:  %s\n",
		factRTOSecondsText(facts.EstimatedRTOSeconds),
	)
	fmt.Fprintf(
		out,
		"  ReasonCodes:   %s\n",
		emptyDash(strings.Join(facts.ReasonCodes, ", ")),
	)
	if len(facts.EnvelopeReasons) > 0 {
		fmt.Fprintf(
			out,
			"  EnvelopeReasons: %s\n",
			strings.Join(facts.EnvelopeReasons, ", "),
		)
	}
	if facts.Ambiguous {
		fmt.Fprintf(
			out,
			"  Selected:      none (%d candidate records, ambiguous)\n",
			facts.Candidates,
		)
	} else {
		fmt.Fprintf(
			out,
			"  Selected:      %s (%d candidate records)\n",
			emptyDash(facts.SelectedRecord),
			facts.Candidates,
		)
	}
	fmt.Fprintf(out, "  Conformance:   %s\n", conformanceText(facts.Conformance))
	for _, violation := range facts.Conformance.Violations {
		fmt.Fprintf(out, "  - violation:   %s\n", violation)
	}
}

func conformanceText(report kueueadapter.ConformanceReport) string {
	if report.Valid {
		return "valid"
	}
	return fmt.Sprintf("invalid (%d violations)", len(report.Violations))
}

func factBoolText(value *bool) string {
	if value == nil {
		return kueueadapter.FactValueUnknown
	}
	if *value {
		return "true"
	}
	return "false"
}

func factRTOSecondsText(value *int64) string {
	if value == nil {
		return kueueadapter.FactValueUnknown
	}
	return strconv.FormatInt(*value, 10)
}

func errToString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// kueueExplainOutput is the stable JSON contract of `kueue explain`. Consumers
// read schemaVersion before anything else.
type kueueExplainOutput struct {
	SchemaVersion     string                        `json:"schemaVersion"`
	Workload          *unstructured.Unstructured    `json:"workload"`
	ProtectionEnabled bool                          `json:"protectionEnabled"`
	CheckpointFacts   *kueueadapter.CheckpointFacts `json:"checkpointFacts,omitempty"`
	FactsError        string                        `json:"factsError,omitempty"`
}

type kueueCondition struct {
	Type   string
	Status string
	Reason string
}

func kueueConditions(workload *unstructured.Unstructured) []kueueCondition {
	raw, found, _ := unstructured.NestedSlice(
		workload.Object,
		"status",
		"conditions",
	)
	if !found {
		return nil
	}
	result := make([]kueueCondition, 0, len(raw))
	for _, item := range raw {
		condition, ok := item.(map[string]any)
		if !ok {
			continue
		}
		result = append(result, kueueCondition{
			Type:   stringValue(condition["type"]),
			Status: stringValue(condition["status"]),
			Reason: stringValue(condition["reason"]),
		})
	}
	return result
}

func kueueWorkloadState(workload *unstructured.Unstructured) string {
	for _, condition := range kueueConditions(workload) {
		if condition.Status == "True" {
			return condition.Type
		}
	}
	return "-"
}

func labelValue(workload *unstructured.Unstructured, key string) string {
	return workload.GetLabels()[key]
}

func stringValue(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return strings.TrimSpace(fmt.Sprint(value))
}
