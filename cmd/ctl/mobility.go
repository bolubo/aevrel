package main

import (
	"context"
	"fmt"
	"io"
	"sort"
	"text/tabwriter"

	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func executeGetMobilityTransactions(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	out io.Writer,
) error {
	var list shardferryv1.MobilityTransactionList
	if err := kubeClient.List(
		ctx,
		&list,
		client.InNamespace(namespace),
	); err != nil {
		return err
	}
	sort.Slice(list.Items, func(i, j int) bool {
		return list.Items[i].Name < list.Items[j].Name
	})
	if jsonOut {
		return writeJSON(out, list.Items)
	}
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(
		writer,
		"NAME\tMODE\tPHASE\tMIGRATION\tTARGET_READY\tPROOF",
	)
	for _, item := range list.Items {
		migration := item.Spec.MigrationRef
		if migration == "" {
			migration = item.Name
		}
		targetReady := "unknown"
		if item.Status.TargetReady != nil {
			targetReady = fmt.Sprintf("%t", item.Status.TargetReady.Ready)
		}
		fmt.Fprintf(
			writer,
			"%s\t%s\t%s\t%s\t%s\t%s\n",
			item.Name,
			emptyDash(item.Spec.Mode),
			emptyDash(item.Status.Phase),
			emptyDash(migration),
			targetReady,
			emptyDash(item.Status.ProofRef),
		)
	}
	return nil
}

func executeGetMobilityTransaction(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
	jsonOut bool,
	out io.Writer,
) error {
	var transaction shardferryv1.MobilityTransaction
	if err := kubeClient.Get(ctx, client.ObjectKey{
		Namespace: namespace,
		Name:      name,
	}, &transaction); err != nil {
		return err
	}
	if jsonOut {
		return writeJSON(out, &transaction)
	}
	migration := transaction.Spec.MigrationRef
	if migration == "" {
		migration = transaction.Name
	}
	fmt.Fprintf(out, "Name:        %s\n", transaction.Name)
	fmt.Fprintf(out, "Namespace:   %s\n", transaction.Namespace)
	fmt.Fprintf(out, "Record:      %s\n", emptyDash(transaction.Spec.RecordRef))
	fmt.Fprintf(out, "Mode:        %s\n", emptyDash(transaction.Spec.Mode))
	fmt.Fprintf(out, "Migration:   %s\n", emptyDash(migration))
	fmt.Fprintf(out, "Phase:       %s\n", emptyDash(transaction.Status.Phase))
	fmt.Fprintf(
		out,
		"Migration phase: %s\n",
		emptyDash(transaction.Status.MigrationPhase),
	)
	fmt.Fprintf(out, "Proof:       %s\n", emptyDash(transaction.Status.ProofRef))
	fmt.Fprintf(out, "Message:     %s\n", emptyDash(transaction.Status.Message))
	if transaction.Status.TargetReady != nil {
		ready := transaction.Status.TargetReady
		fmt.Fprintf(
			out,
			"Target:      ready=%t ranks=%d/%d steps=%d condition=%s\n",
			ready.Ready,
			ready.RanksReady,
			ready.RanksTotal,
			ready.StepsRan,
			emptyDash(ready.Condition),
		)
	}
	if transaction.Status.Phase != shardferryv1.TransactionSucceeded {
		fmt.Fprintln(
			out,
			"Next: inspect the referenced CheckpointMigration and target-ready condition",
		)
	}
	return nil
}
