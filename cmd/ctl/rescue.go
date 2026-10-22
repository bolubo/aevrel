package main

import (
	"context"
	"fmt"
	"io"
	"sort"
	"text/tabwriter"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func executeRescue(
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
	spec := shardferryv1.RescueDecisionSpec{Fallback: "hold"}
	name := ""
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--event":
			index++
			if index >= len(args) {
				return fmt.Errorf("--event requires a value")
			}
			spec.Event = args[index]
		case "--workload":
			index++
			if index >= len(args) {
				return fmt.Errorf("--workload requires a value")
			}
			spec.WorkloadRef = args[index]
		case "--checkpoint":
			index++
			if index >= len(args) {
				return fmt.Errorf("--checkpoint requires a value")
			}
			spec.CheckpointRef = args[index]
		case "--target":
			index++
			if index >= len(args) {
				return fmt.Errorf("--target requires a value")
			}
			spec.Target = args[index]
		case "--fallback":
			index++
			if index >= len(args) {
				return fmt.Errorf("--fallback requires a value")
			}
			spec.Fallback = args[index]
		case "--name":
			index++
			if index >= len(args) {
				return fmt.Errorf("--name requires a value")
			}
			name = args[index]
		default:
			return fmt.Errorf("unknown rescue option: %s", args[index])
		}
	}
	if spec.Event == "" {
		return fmt.Errorf("--event is required")
	}
	if spec.Fallback != "hold" && spec.Fallback != "restart" {
		return fmt.Errorf("--fallback must be hold or restart")
	}
	decision := &shardferryv1.RescueDecision{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: spec,
	}
	if name == "" {
		decision.GenerateName = "rescue-"
	}
	if err := kubeClient.Create(ctx, decision); err != nil {
		return err
	}
	if jsonOut {
		return writeJSON(out, decision)
	}
	fmt.Fprintf(
		out,
		"created rescue decision %s/%s\n",
		decision.Namespace,
		decision.Name,
	)
	fmt.Fprintln(out, "Next: shardferryctl get rescuedecision "+decision.Name)
	return nil
}

func executeGetRescueDecisions(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	out io.Writer,
) error {
	var list shardferryv1.RescueDecisionList
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
		"NAME\tEVENT\tACTION\tCODE\tWORKLOAD\tTARGET\tDECIDED_AT",
	)
	for _, item := range list.Items {
		fmt.Fprintf(
			writer,
			"%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			item.Name,
			emptyDash(item.Spec.Event),
			emptyDash(item.Status.Action),
			emptyDash(item.Status.ReasonCode),
			emptyDash(item.Spec.WorkloadRef),
			emptyDash(item.Status.Target),
			emptyDash(item.Status.DecidedAt),
		)
	}
	return nil
}

func executeGetRescueDecision(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
	jsonOut bool,
	out io.Writer,
) error {
	var decision shardferryv1.RescueDecision
	if err := kubeClient.Get(ctx, client.ObjectKey{
		Namespace: namespace,
		Name:      name,
	}, &decision); err != nil {
		return err
	}
	if jsonOut {
		return writeJSON(out, &decision)
	}
	fmt.Fprintf(out, "Name:       %s\n", decision.Name)
	fmt.Fprintf(out, "Namespace:  %s\n", decision.Namespace)
	fmt.Fprintf(out, "Event:      %s\n", emptyDash(decision.Spec.Event))
	fmt.Fprintf(out, "Workload:   %s\n", emptyDash(decision.Spec.WorkloadRef))
	fmt.Fprintf(out, "Checkpoint: %s\n", emptyDash(decision.Spec.CheckpointRef))
	fmt.Fprintf(out, "Action:     %s\n", emptyDash(decision.Status.Action))
	fmt.Fprintf(out, "Code:       %s\n", emptyDash(decision.Status.ReasonCode))
	fmt.Fprintf(out, "Reason:     %s\n", emptyDash(decision.Status.Reason))
	fmt.Fprintf(out, "Target:     %s\n", emptyDash(decision.Status.Target))
	fmt.Fprintf(out, "Proof:      %s\n", emptyDash(decision.Status.ProofRef))
	if decision.Status.Action == "hold" {
		fmt.Fprintln(
			out,
			"Next: provide a fresh, loadable CheckpointRecord or inspect the drill failure",
		)
	}
	return nil
}
