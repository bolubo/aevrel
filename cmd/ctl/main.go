package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"text/tabwriter"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func main() {
	namespace, jsonOut, args, err := parseGlobal(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if containsHelp(args) {
		printUsage(os.Stdout)
		return
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	var kubeClient client.Client
	if needsKubeClient(args) {
		config, err := ctrl.GetConfig()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		kubeClient, err = client.New(config, client.Options{Scheme: scheme})
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	if err := execute(
		context.Background(),
		kubeClient,
		namespace,
		jsonOut,
		args,
		os.Stdout,
	); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func containsHelp(args []string) bool {
	for _, arg := range args {
		if arg == "-h" || arg == "--help" {
			return true
		}
	}
	return false
}

func needsKubeClient(args []string) bool {
	if len(args) == 0 {
		return false
	}
	if args[0] == "migrate" {
		for _, arg := range args[1:] {
			if arg == "--dry-run" {
				return false
			}
		}
	}
	return true
}

func printUsage(out io.Writer) {
	fmt.Fprintln(out, "usage: shardferryctl migrate -f FILE [--wait|--dry-run]")
	fmt.Fprintln(out, "       shardferryctl get migrations|migration NAME")
	fmt.Fprintln(out, "       shardferryctl describe migration NAME")
	fmt.Fprintln(out, "       shardferryctl cancel|retry migration NAME")
	fmt.Fprintln(out, "global: -n/--namespace NAMESPACE, --json")
}

func execute(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: shardferryctl migrate|get|describe|cancel|retry")
	}
	switch args[0] {
	case "migrate":
		return executeMigrate(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "get":
		return executeGet(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "describe":
		return executeDescribe(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "cancel":
		return executeCancel(ctx, kubeClient, namespace, args[1:], out)
	case "retry":
		return executeRetry(ctx, kubeClient, namespace, args[1:], out)
	default:
		return fmt.Errorf("unknown command: %s", args[0])
	}
}

func executeGet(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: shardferryctl get migrations|migration NAME")
	}
	switch args[0] {
	case "migrations":
		var list shardferryv1.CheckpointMigrationList
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
		return writeTable(out, list.Items)
	case "migration":
		if len(args) < 2 {
			return fmt.Errorf("usage: shardferryctl get migration NAME")
		}
		migration, err := getMigration(ctx, kubeClient, namespace, args[1])
		if err != nil {
			return err
		}
		if jsonOut {
			return writeJSON(out, migration)
		}
		return writeTable(out, []shardferryv1.CheckpointMigration{*migration})
	default:
		return fmt.Errorf("unknown resource: %s", args[0])
	}
}

func executeDescribe(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	if len(args) < 2 || args[0] != "migration" {
		return fmt.Errorf("usage: shardferryctl describe migration NAME")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if jsonOut {
		return writeJSON(out, migration)
	}
	return writeDescribe(out, migration)
}

func executeCancel(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	args []string,
	out io.Writer,
) error {
	if len(args) < 2 || args[0] != "migration" {
		return fmt.Errorf("usage: shardferryctl cancel migration NAME")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if migration.Annotations == nil {
		migration.Annotations = make(map[string]string)
	}
	migration.Annotations["shardferry.io/cancel"] = "true"
	if err := kubeClient.Update(ctx, migration); err != nil {
		return err
	}
	fmt.Fprintf(out, "cancellation requested for %s/%s\n", namespace, args[1])
	return nil
}

func executeRetry(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	args []string,
	out io.Writer,
) error {
	if len(args) < 2 || args[0] != "migration" {
		return fmt.Errorf("usage: shardferryctl retry migration NAME")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if migration.Annotations == nil {
		migration.Annotations = make(map[string]string)
	}
	migration.Annotations["shardferry.io/retry"] = "true"
	if err := kubeClient.Update(ctx, migration); err != nil {
		return err
	}
	fmt.Fprintf(out, "retry requested for %s/%s\n", namespace, args[1])
	return nil
}

func getMigration(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
) (*shardferryv1.CheckpointMigration, error) {
	var migration shardferryv1.CheckpointMigration
	if err := kubeClient.Get(
		ctx,
		client.ObjectKey{Namespace: namespace, Name: name},
		&migration,
	); err != nil {
		return nil, err
	}
	return &migration, nil
}

func writeTable(
	out io.Writer,
	migrations []shardferryv1.CheckpointMigration,
) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(writer, "NAME\tPHASE\tTARGET\tSTAGED\tCOMMITTED\tFAILED\tAGE")
	for index := range migrations {
		migration := &migrations[index]
		fmt.Fprintf(
			writer,
			"%s\t%s\t%d\t%d\t%d\t%d\t%s\n",
			migration.Name,
			migration.Status.Phase,
			migration.Spec.Target.WorldSize,
			migration.Status.Counts.Staged,
			migration.Status.Counts.Committed,
			migration.Status.Counts.Failed,
			age(migration.CreationTimestamp.Time),
		)
	}
	return nil
}

func writeDescribe(
	out io.Writer,
	migration *shardferryv1.CheckpointMigration,
) error {
	fmt.Fprintf(out, "Name:       %s\n", migration.Name)
	fmt.Fprintf(out, "Namespace:  %s\n", migration.Namespace)
	fmt.Fprintf(out, "Phase:      %s\n", migration.Status.Phase)
	fmt.Fprintf(
		out,
		"Source:     %d rank(s), mesh %v, root %s\n",
		migration.Spec.Source.WorldSize,
		migration.Spec.Source.Mesh.Shape,
		migration.Spec.Source.CheckpointRoot,
	)
	fmt.Fprintf(
		out,
		"Target:     %d rank(s), mesh %v\n",
		migration.Spec.Target.WorldSize,
		migration.Spec.Target.Mesh.Shape,
	)
	fmt.Fprintf(
		out,
		"Ranks:      prepared=%d staged=%d committed=%d failed=%d\n",
		migration.Status.Counts.Prepared,
		migration.Status.Counts.Staged,
		migration.Status.Counts.Committed,
		migration.Status.Counts.Failed,
	)
	fmt.Fprintf(out, "PlanHash:   %s\n", emptyDash(migration.Status.PlanHash))
	fmt.Fprintf(out, "Execution:  %s\n", emptyDash(migration.Status.ExecutionRef))
	fmt.Fprintf(out, "Report:     %s\n", emptyDash(migration.Status.ReportRef))
	fmt.Fprintf(out, "Proof:      %s\n", emptyDash(migration.Status.ProofRef))
	fmt.Fprintf(out, "Proof hash: %s\n", emptyDash(migration.Status.ProofHash))
	if len(migration.Status.FailedRanks) > 0 {
		fmt.Fprintf(out, "FailedRanks: %v\n", migration.Status.FailedRanks)
	}
	for _, condition := range migration.Status.Conditions {
		fmt.Fprintf(
			out,
			"Condition:  %s=%s reason=%s message=%s\n",
			condition.Type,
			condition.Status,
			condition.Reason,
			condition.Message,
		)
	}
	return nil
}

func writeJSON(out io.Writer, value any) error {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func emptyDash(value string) string {
	if value == "" {
		return "-"
	}
	return value
}

func age(created time.Time) string {
	if created.IsZero() {
		return "-"
	}
	duration := time.Since(created)
	if duration < time.Minute {
		return duration.Round(time.Second).String()
	}
	return duration.Round(time.Minute).String()
}

func parseGlobal(args []string) (string, bool, []string, error) {
	namespace := "default"
	jsonOut := false
	remaining := make([]string, 0, len(args))
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--namespace", "-n":
			if index+1 >= len(args) {
				return "", false, nil, fmt.Errorf("--namespace requires a value")
			}
			namespace = args[index+1]
			index++
		case "--json":
			jsonOut = true
		default:
			remaining = append(remaining, args[index])
		}
	}
	return namespace, jsonOut, remaining, nil
}
