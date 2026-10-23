package main

import (
	"context"
	"fmt"
	"io"
	"sort"
	"text/tabwriter"

	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func executeReadiness(
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
	if len(args) > 1 {
		return fmt.Errorf("usage: aevrelctl readiness [NAME]")
	}
	if len(args) == 1 {
		var record aevrelv1.CheckpointRecord
		if err := kubeClient.Get(ctx, client.ObjectKey{
			Namespace: namespace,
			Name:      args[0],
		}, &record); err != nil {
			return err
		}
		if jsonOut {
			return writeJSON(out, &record)
		}
		return writeReadiness(out, &record)
	}

	var records aevrelv1.CheckpointRecordList
	if err := kubeClient.List(
		ctx,
		&records,
		client.InNamespace(namespace),
	); err != nil {
		return err
	}
	sort.Slice(records.Items, func(i, j int) bool {
		return records.Items[i].Name < records.Items[j].Name
	})
	if jsonOut {
		return writeJSON(out, records.Items)
	}
	writer := newReadinessTable(out)
	defer writer.Flush()
	for index := range records.Items {
		record := &records.Items[index]
		fmt.Fprintf(
			writer,
			"%s\t%s\t%t\t%s\t%s\n",
			record.Name,
			emptyDash(record.Status.Readiness.Status),
			record.Status.Readiness.Complete,
			emptyDash(record.Status.Readiness.LastDrill),
			emptyDash(record.Status.Readiness.ObservedAt),
		)
	}
	return nil
}

func writeReadiness(out io.Writer, record *aevrelv1.CheckpointRecord) error {
	fmt.Fprintf(out, "Name:         %s\n", record.Name)
	fmt.Fprintf(out, "Namespace:    %s\n", record.Namespace)
	fmt.Fprintf(out, "Checkpoint:   %s\n", record.Spec.Checkpoint)
	fmt.Fprintf(out, "Provider:     %s\n", emptyDash(record.Spec.Provider))
	fmt.Fprintf(out, "Version:      %s\n", emptyDash(record.Spec.Version))
	fmt.Fprintf(out, "Status:       %s\n", emptyDash(record.Status.Readiness.Status))
	fmt.Fprintf(out, "Complete:     %t\n", record.Status.Readiness.Complete)
	fmt.Fprintf(out, "Fresh:        %t\n", record.Status.Readiness.Fresh)
	fmt.Fprintf(out, "Loadable:     %s\n", boolValueText(record.Status.Readiness.Loadable))
	fmt.Fprintf(out, "Movable:      %s\n", boolValueText(record.Status.Readiness.Movable))
	fmt.Fprintf(out, "Last drill:   %s\n", emptyDash(record.Status.Readiness.LastDrill))
	if record.Status.Readiness.LastDrillVersion != "" {
		fmt.Fprintf(
			out,
			"Drill version: %s\n",
			record.Status.Readiness.LastDrillVersion,
		)
	}
	if record.Status.Readiness.LastDrillDigest != "" {
		fmt.Fprintf(
			out,
			"Drill digest:  %s\n",
			record.Status.Readiness.LastDrillDigest,
		)
	}
	for _, reason := range record.Status.Readiness.Reasons {
		fmt.Fprintf(out, "Reason:       %s: %s\n", reason.Code, reason.Detail)
	}
	if record.Status.Readiness.Status != aevrelv1.ReadinessReady {
		fmt.Fprintln(out, "Next: run a restore drill / target load before treating this checkpoint as recoverable")
	}
	return nil
}

func newReadinessTable(out io.Writer) *tabwriter.Writer {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	fmt.Fprintln(writer, "NAME\tSTATUS\tCOMPLETE\tLAST_DRILL\tOBSERVED_AT")
	return writer
}

func boolValueText(value *bool) string {
	if value == nil {
		return "unknown"
	}
	return fmt.Sprintf("%t", *value)
}
