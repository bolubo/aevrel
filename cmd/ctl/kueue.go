package main

import (
	"context"
	"fmt"
	"io"
	"sort"
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
	if jsonOut {
		return writeJSON(out, workload)
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
	return nil
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
