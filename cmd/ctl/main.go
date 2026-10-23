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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/clusterclient"
	"github.com/bolubo/aevrel/internal/kueueadapter"
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
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	kueueadapter.AddToScheme(scheme, kueueadapter.DefaultWorkloadGVK)
	var kubeClient client.Client
	if needsKubeClient(args) {
		config, err := ctrl.GetConfig()
		if err != nil {
			fmt.Fprintf(
				os.Stderr,
				"aevrelctl %s requires a Kubernetes kubeconfig: %v\n"+
					"For offline spec validation, run:\n"+
					"  aevrelctl migrate -f FILE --dry-run\n",
				args[0],
				err,
			)
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
	if args[0] == "cluster" && len(args) > 1 && args[1] == "add" {
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
	fmt.Fprintln(out, "usage: aevrelctl migrate -f FILE [--wait|--dry-run] [--no-progress] [--max-in-flight N]")
	fmt.Fprintln(out, "       aevrelctl plan -f FILE [--max-in-flight N]")
	fmt.Fprintln(out, "       aevrelctl preflight -f FILE [--max-in-flight N]")
	fmt.Fprintln(out, "       aevrelctl readiness [NAME]")
	fmt.Fprintln(out, "       aevrelctl kueue status|explain NAME")
	fmt.Fprintln(out, "       aevrelctl rescue --event REASON [--workload NAME] [--checkpoint NAME] [--target TARGET] [--fallback hold|restart]")
	fmt.Fprintln(out, "       aevrelctl get rescuedecisions|rescuedecision NAME")
	fmt.Fprintln(out, "       aevrelctl get migrations|migration NAME")
	fmt.Fprintln(out, "       aevrelctl get clusters|cluster NAME")
	fmt.Fprintln(out, "       aevrelctl describe migration NAME")
	fmt.Fprintln(out, "       aevrelctl describe cluster NAME")
	fmt.Fprintln(out, "       aevrelctl cluster add NAME --endpoint URL [--role source|target|both] [--file PATH]")
	fmt.Fprintln(out, "       aevrelctl delete migration NAME [--purge-target] [--dry-run]")
	fmt.Fprintln(out, "       aevrelctl cancel|retry migration NAME")
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
		return fmt.Errorf("usage: aevrelctl migrate|get|describe|cancel|retry")
	}
	switch args[0] {
	case "migrate":
		return executeMigrate(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "plan":
		return executePlan(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "preflight":
		return executePreflight(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "readiness":
		return executeReadiness(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "kueue":
		return executeKueue(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "rescue":
		return executeRescue(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "get":
		return executeGet(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "describe":
		return executeDescribe(ctx, kubeClient, namespace, jsonOut, args[1:], out)
	case "cluster":
		if len(args) >= 2 && args[1] == "add" {
			return executeClusterAdd(jsonOut, namespace, args[2:], out)
		}
		return fmt.Errorf("usage: aevrelctl cluster add NAME --endpoint URL")
	case "delete":
		return executeDelete(ctx, kubeClient, namespace, args[1:], out)
	case "cancel":
		return executeCancel(ctx, kubeClient, namespace, args[1:], out)
	case "retry":
		return executeRetry(ctx, kubeClient, namespace, args[1:], out)
	default:
		return fmt.Errorf("unknown command: %s", args[0])
	}
}

func executeDelete(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	args []string,
	out io.Writer,
) error {
	if len(args) < 2 || args[0] != "migration" {
		return fmt.Errorf("usage: aevrelctl delete migration NAME [--purge-target] [--dry-run]")
	}
	purgeTarget := false
	dryRun := false
	for _, arg := range args[2:] {
		switch arg {
		case "--purge-target":
			purgeTarget = true
		case "--keep-target":
			purgeTarget = false
		case "--dry-run":
			dryRun = true
		default:
			return fmt.Errorf("unknown delete option: %s", arg)
		}
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if dryRun {
		fmt.Fprintf(out, "would delete migration %s/%s\n", namespace, args[1])
		fmt.Fprintf(out, "phase: %s\n", emptyDash(migration.Status.Phase))
		fmt.Fprintf(out, "target root template: %s\n", migration.Spec.Target.RootTemplate)
		fmt.Fprintf(out, "purge target: %t\n", purgeTarget)
		return nil
	}
	if migration.Annotations == nil {
		migration.Annotations = make(map[string]string)
	}
	if purgeTarget {
		migration.Annotations["aevrel.dev/purge-target"] = "true"
	}
	if !containsFinalizer(migration, "aevrel.dev/cleanup") {
		migration.Finalizers = append(migration.Finalizers, "aevrel.dev/cleanup")
	}
	if err := kubeClient.Update(ctx, migration); err != nil {
		return err
	}
	if err := kubeClient.Delete(ctx, migration); err != nil {
		return err
	}
	fmt.Fprintf(out, "deletion requested for %s/%s (purge_target=%t)\n", namespace, args[1], purgeTarget)
	return nil
}

func containsFinalizer(
	migration *aevrelv1.CheckpointMigration,
	name string,
) bool {
	for _, finalizer := range migration.Finalizers {
		if finalizer == name {
			return true
		}
	}
	return false
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
		return fmt.Errorf("usage: aevrelctl get RESOURCE [NAME]")
	}
	switch args[0] {
	case "migrations":
		var list aevrelv1.CheckpointMigrationList
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
			return fmt.Errorf("usage: aevrelctl get migration NAME")
		}
		migration, err := getMigration(ctx, kubeClient, namespace, args[1])
		if err != nil {
			return err
		}
		if jsonOut {
			return writeJSON(out, migration)
		}
		return writeTable(out, []aevrelv1.CheckpointMigration{*migration})
	case "clusters":
		return executeGetClusters(ctx, kubeClient, namespace, jsonOut, out)
	case "cluster":
		if len(args) < 2 {
			return fmt.Errorf("usage: aevrelctl get cluster NAME")
		}
		return executeGetCluster(ctx, kubeClient, namespace, args[1], jsonOut, out)
	case "rescuedecisions":
		return executeGetRescueDecisions(ctx, kubeClient, namespace, jsonOut, out)
	case "rescuedecision":
		if len(args) < 2 {
			return fmt.Errorf("usage: aevrelctl get rescuedecision NAME")
		}
		return executeGetRescueDecision(
			ctx,
			kubeClient,
			namespace,
			args[1],
			jsonOut,
			out,
		)
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
	showRanks := false
	positional := make([]string, 0, len(args))
	for _, arg := range args {
		if arg == "--ranks" {
			showRanks = true
			continue
		}
		positional = append(positional, arg)
	}
	if len(positional) < 2 {
		return fmt.Errorf("usage: aevrelctl describe migration|cluster NAME [--ranks]")
	}
	if positional[0] == "cluster" {
		return executeDescribeCluster(ctx, kubeClient, namespace, positional[1], jsonOut, out)
	}
	if positional[0] != "migration" {
		return fmt.Errorf("usage: aevrelctl describe migration|cluster NAME [--ranks]")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, positional[1])
	if err != nil {
		return err
	}
	if jsonOut {
		return writeJSON(out, migration)
	}
	return writeDescribe(out, migration, showRanks)
}

func executeGetClusters(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	out io.Writer,
) error {
	var list aevrelv1.AevrelClusterList
	if err := kubeClient.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		return err
	}
	sort.Slice(list.Items, func(i, j int) bool {
		return list.Items[i].Name < list.Items[j].Name
	})
	views := make([]clusterView, 0, len(list.Items))
	for index := range list.Items {
		views = append(views, inspectCluster(ctx, kubeClient, &list.Items[index]))
	}
	if jsonOut {
		return writeJSON(out, views)
	}
	return writeClusterTable(out, views)
}

func executeGetCluster(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
	jsonOut bool,
	out io.Writer,
) error {
	cluster, err := getCluster(ctx, kubeClient, namespace, name)
	if err != nil {
		return err
	}
	view := inspectCluster(ctx, kubeClient, cluster)
	if jsonOut {
		return writeJSON(out, view)
	}
	return writeClusterTable(out, []clusterView{view})
}

func executeDescribeCluster(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
	jsonOut bool,
	out io.Writer,
) error {
	cluster, err := getCluster(ctx, kubeClient, namespace, name)
	if err != nil {
		return err
	}
	view := inspectCluster(ctx, kubeClient, cluster)
	if jsonOut {
		return writeJSON(out, view)
	}
	return writeClusterDescribe(out, view)
}

type clusterView struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	Role      string `json:"role,omitempty"`
	Endpoint  string `json:"endpoint"`
	Ready     bool   `json:"ready"`
	Version   string `json:"version,omitempty"`
	Cluster   string `json:"cluster,omitempty"`
	Error     string `json:"error,omitempty"`
}

func inspectCluster(
	ctx context.Context,
	kubeClient client.Client,
	cluster *aevrelv1.AevrelCluster,
) clusterView {
	view := clusterView{
		Name:      cluster.Name,
		Namespace: cluster.Namespace,
		Role:      cluster.Spec.Role,
		Endpoint:  cluster.Spec.Endpoint,
	}
	token, err := clusterToken(ctx, kubeClient, cluster.Namespace, cluster.Spec.SecretRef)
	if err != nil {
		view.Error = err.Error()
		return view
	}
	client := newClusterClient()
	health, err := client.Health(ctx, cluster.Spec.Endpoint, token)
	if err != nil {
		view.Error = err.Error()
		return view
	}
	view.Ready = true
	view.Version = health.Version
	view.Cluster = health.Cluster
	return view
}

func newClusterClient() *clusterclient.Client {
	ca := os.Getenv("AEVREL_TLS_CA")
	cert := os.Getenv("AEVREL_TLS_CERT")
	key := os.Getenv("AEVREL_TLS_KEY")
	if ca != "" || cert != "" || key != "" {
		if client, err := clusterclient.NewWithTLS("", ca, cert, key); err == nil {
			return client
		}
	}
	return clusterclient.New("")
}

func getCluster(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	name string,
) (*aevrelv1.AevrelCluster, error) {
	var cluster aevrelv1.AevrelCluster
	if err := kubeClient.Get(
		ctx,
		client.ObjectKey{Namespace: namespace, Name: name},
		&cluster,
	); err != nil {
		return nil, err
	}
	return &cluster, nil
}

func clusterToken(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	ref aevrelv1.ClusterSecretRef,
) (string, error) {
	if ref.Name == "" {
		return "", fmt.Errorf("cluster secretRef.name is required")
	}
	key := ref.Key
	if key == "" {
		key = "token"
	}
	var secret corev1.Secret
	if err := kubeClient.Get(
		ctx,
		client.ObjectKey{Namespace: namespace, Name: ref.Name},
		&secret,
	); err != nil {
		return "", err
	}
	value := secret.Data[key]
	if len(value) == 0 {
		return "", fmt.Errorf("secret %s/%s key %s is empty", namespace, ref.Name, key)
	}
	return string(value), nil
}

func writeClusterTable(out io.Writer, views []clusterView) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(writer, "NAME\tROLE\tENDPOINT\tREADY\tVERSION\tERROR")
	for _, view := range views {
		ready := "false"
		if view.Ready {
			ready = "true"
		}
		fmt.Fprintf(
			writer,
			"%s\t%s\t%s\t%s\t%s\t%s\n",
			view.Name,
			emptyDash(view.Role),
			view.Endpoint,
			ready,
			emptyDash(view.Version),
			emptyDash(view.Error),
		)
	}
	return nil
}

func writeClusterDescribe(out io.Writer, view clusterView) error {
	fmt.Fprintf(out, "Name:       %s\n", view.Name)
	fmt.Fprintf(out, "Namespace:  %s\n", view.Namespace)
	fmt.Fprintf(out, "Role:       %s\n", emptyDash(view.Role))
	fmt.Fprintf(out, "Endpoint:   %s\n", view.Endpoint)
	fmt.Fprintf(out, "Ready:      %t\n", view.Ready)
	fmt.Fprintf(out, "Cluster:    %s\n", emptyDash(view.Cluster))
	fmt.Fprintf(out, "Version:    %s\n", emptyDash(view.Version))
	if view.Error != "" {
		fmt.Fprintf(out, "Error:      %s\n", view.Error)
	}
	return nil
}

func executeCancel(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	args []string,
	out io.Writer,
) error {
	if len(args) < 2 || args[0] != "migration" {
		return fmt.Errorf("usage: aevrelctl cancel migration NAME")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if migration.Annotations == nil {
		migration.Annotations = make(map[string]string)
	}
	migration.Annotations["aevrel.dev/cancel"] = "true"
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
		return fmt.Errorf("usage: aevrelctl retry migration NAME")
	}
	migration, err := getMigration(ctx, kubeClient, namespace, args[1])
	if err != nil {
		return err
	}
	if migration.Annotations == nil {
		migration.Annotations = make(map[string]string)
	}
	migration.Annotations["aevrel.dev/retry"] = "true"
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
) (*aevrelv1.CheckpointMigration, error) {
	var migration aevrelv1.CheckpointMigration
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
	migrations []aevrelv1.CheckpointMigration,
) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(writer, "NAME\tMODE\tPHASE\tTARGET\tSTAGED\tCOMMITTED\tFAILED\tAGE")
	for index := range migrations {
		migration := &migrations[index]
		mode := migration.Spec.Mode
		if mode == "" {
			mode = aevrelv1.MigrationModeStatic
		}
		fmt.Fprintf(
			writer,
			"%s\t%s\t%s\t%d\t%d\t%d\t%d\t%s\n",
			migration.Name,
			mode,
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
	migration *aevrelv1.CheckpointMigration,
	showRanks bool,
) error {
	fmt.Fprintf(out, "Name:       %s\n", migration.Name)
	fmt.Fprintf(out, "Namespace:  %s\n", migration.Namespace)
	fmt.Fprintf(out, "Phase:      %s\n", migration.Status.Phase)
	mode := migration.Spec.Mode
	if mode == "" {
		mode = aevrelv1.MigrationModeStatic
	}
	fmt.Fprintf(out, "Mode:       %s\n", mode)
	fmt.Fprintf(out, "Next:       %s\n", nextAction(migration))
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
	if live := migration.Spec.Live; live != nil {
		fmt.Fprintf(
			out,
			"Live spec:  maxInFlight=%d stateMode=%s handoff=%s\n",
			live.MaxInFlight,
			live.StateMode,
			emptyDash(live.HandoffEndpoint),
		)
	}
	fmt.Fprintf(out, "PlanHash:   %s\n", emptyDash(migration.Status.PlanHash))
	fmt.Fprintf(out, "Execution:  %s\n", emptyDash(migration.Status.ExecutionRef))
	fmt.Fprintf(out, "Report:     %s\n", emptyDash(migration.Status.ReportRef))
	fmt.Fprintf(out, "Proof:      %s\n", emptyDash(migration.Status.ProofRef))
	fmt.Fprintf(out, "Proof hash: %s\n", emptyDash(migration.Status.ProofHash))
	if target := migration.Status.TargetReady; target != nil {
		fmt.Fprintf(
			out,
			"TargetReady: ready=%t condition=%s version=%s ranks=%d/%d steps=%d stateComplete=%t durationMs=%d\n",
			target.Ready,
			emptyDash(target.Condition),
			emptyDash(target.Version),
			target.RanksReady,
			target.RanksTotal,
			target.StepsRan,
			target.StateComplete,
			target.DurationMilliseconds,
		)
		fmt.Fprintf(
			out,
			"Target proof: %s (%s)\n",
			emptyDash(target.ProofRef),
			emptyDash(target.ProofHash),
		)
	}
	if live := migration.Status.Live; live != nil {
		fmt.Fprintf(
			out,
			"Live:       phase=%s inFlight=%v coverage=%v quorum=%d stateComplete=%t\n",
			emptyDash(live.CutoverPhase),
			live.InFlight,
			live.Coverage,
			live.Quorum,
			live.StateComplete,
		)
		if live.Message != "" {
			fmt.Fprintf(out, "Live msg:   %s\n", live.Message)
		}
		if showRanks && len(live.Ranks) > 0 {
			if err := writeLiveRankTable(out, live.Ranks); err != nil {
				return err
			}
		}
	}
	if len(migration.Status.FailedRanks) > 0 {
		fmt.Fprintf(out, "FailedRanks: %v\n", migration.Status.FailedRanks)
	}
	if showRanks {
		if err := writeRankTable(out, migration.Status.Ranks); err != nil {
			return err
		}
		if migration.Status.TargetReady != nil &&
			len(migration.Status.TargetReady.Ranks) > 0 {
			if err := writeTargetRankTable(
				out,
				migration.Status.TargetReady.Ranks,
			); err != nil {
				return err
			}
		}
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

func writeRankTable(out io.Writer, ranks []aevrelv1.RankStatus) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(writer, "RANK\tPHASE\tTASK\tRETRIES\tERROR")
	for _, rank := range ranks {
		fmt.Fprintf(
			writer,
			"%d\t%s\t%s\t%d\t%s\n",
			rank.Rank,
			rank.Phase,
			emptyDash(rank.TaskID),
			rank.Attempts,
			emptyDash(rank.Error),
		)
	}
	return nil
}

func writeTargetRankTable(
	out io.Writer,
	ranks []aevrelv1.TargetReadyRankStatus,
) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(
		writer,
		"TARGET_RANK\tMODEL\tOPTIMIZER\tSTEPS\tSTATE_COMPLETE\tOK\tERROR",
	)
	for _, rank := range ranks {
		fmt.Fprintf(
			writer,
			"%d\t%t\t%t\t%d\t%t\t%t\t%s\n",
			rank.Rank,
			rank.ModelLoaded,
			rank.OptimizerLoaded,
			rank.StepsRan,
			rank.StateComplete,
			rank.OK,
			emptyDash(rank.Error),
		)
	}
	return nil
}

func writeLiveRankTable(
	out io.Writer,
	ranks []aevrelv1.LiveRankStatus,
) error {
	writer := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	defer writer.Flush()
	fmt.Fprintln(
		writer,
		"LIVE_RANK\tPHASE\tOWNER\tEPOCH\tRETRIES\tERROR",
	)
	for _, rank := range ranks {
		fmt.Fprintf(
			writer,
			"%d\t%s\t%d\t%d\t%d\t%s\n",
			rank.Rank,
			rank.Phase,
			rank.Owner,
			rank.Epoch,
			rank.Retries,
			emptyDash(rank.Error),
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
