package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/clusterclient"
	"github.com/shardferry/shardferry/internal/controller"
)

type planOptions struct {
	file        string
	maxInFlight int32
}

func executePlan(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	options, err := parsePlan(args)
	if err != nil {
		return err
	}
	if kubeClient == nil {
		return fmt.Errorf("Kubernetes client is not configured")
	}
	migration, err := loadMigration(options.file, namespace)
	if err != nil {
		return err
	}
	if err := applyMaxInFlight(migration, options.maxInFlight); err != nil {
		return err
	}
	if err := validateProductMigration(migration); err != nil {
		return fmt.Errorf("invalid migration: %w", err)
	}
	if migration.Spec.Mode == shardferryv1.MigrationModeLiveRankSplit {
		return writeLivePlan(out, jsonOut, migration)
	}
	token := os.Getenv("SHARDFERRY_AGENT_TOKEN")
	if token == "" {
		return fmt.Errorf("SHARDFERRY_AGENT_TOKEN is required for plan")
	}
	agents := agentclient.New(token)
	clusterAPI := clusterclient.New("")
	tlsCA := os.Getenv("SHARDFERRY_TLS_CA")
	tlsCert := os.Getenv("SHARDFERRY_TLS_CERT")
	tlsKey := os.Getenv("SHARDFERRY_TLS_KEY")
	if tlsCA != "" || tlsCert != "" || tlsKey != "" {
		agents, err = agentclient.NewWithTLS(token, tlsCA, tlsCert, tlsKey)
		if err != nil {
			return fmt.Errorf("configure plan agent TLS: %w", err)
		}
		clusterAPI, err = clusterclient.NewWithTLS("", tlsCA, tlsCert, tlsKey)
		if err != nil {
			return fmt.Errorf("configure plan gateway TLS: %w", err)
		}
	}
	resolver := &controller.AgentResolver{
		Client:   kubeClient,
		Agents:   agents,
		Clusters: clusterAPI,
		Tokens:   agents,
	}
	resolved, err := resolver.Resolve(ctx, migration)
	if err != nil {
		return fmt.Errorf("plan resolve: %w", err)
	}
	orchestrator := &controller.Orchestrator{Agents: agents}
	prepared, err := orchestrator.Prepare(ctx, resolved)
	if err != nil {
		return fmt.Errorf("plan build: %w", err)
	}
	summary := planSummary{
		Name:        migration.Name,
		Namespace:   migration.Namespace,
		SourceHost:  migration.Spec.Source.ClusterRef,
		TargetHost:  migration.Spec.Target.ClusterRef,
		SourceRanks: len(resolved.Spec.Source.RankMap),
		TargetRanks: len(resolved.Spec.Target.RankMap),
		PlanHash:    prepared.PlanHash,
		Bytes:       int64Value(prepared.Plan["total_bytes"]),
		MinBytes:    int64Value(prepared.Plan["theoretical_min_bytes"]),
		FullGather:  boolValue(prepared.Plan["full_gather"]),
		NodeCount:   len(anySlice(prepared.Plan["nodes"])),
	}
	if cost, ok := prepared.Plan["cost"].(map[string]any); ok {
		summary.RangeReads = int64Value(cost["range_reads"])
		summary.MaxFanIn = int64Value(cost["max_fan_in"])
	}
	for _, entry := range resolved.Spec.Target.RankMap {
		root := strings.ReplaceAll(
			migration.Spec.Target.RootTemplate,
			"{rank}",
			strconv.Itoa(int(entry.Rank)),
		)
		if len(summary.TargetRoots) == 0 ||
			summary.TargetRoots[len(summary.TargetRoots)-1] != root {
			summary.TargetRoots = append(summary.TargetRoots, root)
		}
	}
	if jsonOut {
		return writeJSON(out, summary)
	}
	return writePlanSummary(out, summary)
}

func parsePlan(args []string) (planOptions, error) {
	options := planOptions{}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "-f", "--file":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("%s requires a path", args[index-1])
			}
			options.file = args[index]
		case "--dry-run":
			return options, fmt.Errorf(
				"plan is read-only; use `shardferryctl migrate -f FILE --dry-run` for offline spec validation",
			)
		case "--max-in-flight":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--max-in-flight requires a number")
			}
			value, err := parseMaxInFlight(args[index])
			if err != nil {
				return options, err
			}
			options.maxInFlight = value
		case "-h", "--help":
			return options, fmt.Errorf(
				"usage: shardferryctl plan -f migration.yaml [--max-in-flight N]",
			)
		default:
			return options, fmt.Errorf("unknown plan option: %s", args[index])
		}
	}
	if options.file == "" {
		return options, fmt.Errorf("plan requires -f migration.yaml")
	}
	return options, nil
}

type planSummary struct {
	Name        string   `json:"name"`
	Namespace   string   `json:"namespace"`
	SourceHost  string   `json:"sourceCluster,omitempty"`
	TargetHost  string   `json:"targetCluster,omitempty"`
	SourceRanks int      `json:"sourceRanks"`
	TargetRanks int      `json:"targetRanks"`
	PlanHash    string   `json:"planHash"`
	Bytes       int64    `json:"bytes"`
	MinBytes    int64    `json:"theoreticalMinBytes"`
	FullGather  bool     `json:"fullGather"`
	NodeCount   int      `json:"nodes"`
	RangeReads  int64    `json:"rangeReads"`
	MaxFanIn    int64    `json:"maxFanIn"`
	TargetRoots []string `json:"targetRoots"`
}

type livePlanSummary struct {
	Name           string   `json:"name"`
	Namespace      string   `json:"namespace"`
	Mode           string   `json:"mode"`
	HandoffURL     string   `json:"handoffEndpoint"`
	MaxInFlight    int32    `json:"maxInFlight"`
	StateMode      string   `json:"stateMode"`
	StateRoot      string   `json:"stateRoot"`
	Quorum         int32    `json:"quorum"`
	RankPlan       string   `json:"rankPlan,omitempty"`
	StateProviders []string `json:"stateProviders,omitempty"`
}

func writeLivePlan(
	out io.Writer,
	jsonOut bool,
	migration *shardferryv1.CheckpointMigration,
) error {
	live := migration.Spec.Live
	if live == nil {
		return fmt.Errorf("live-rank-split spec is missing")
	}
	rankPlan := ""
	if live.RankPlan != nil {
		rankPlan = fmt.Sprintf(
			"tp=%d pp=%d ranks=%v",
			live.RankPlan.TP,
			live.RankPlan.PP,
			live.RankPlan.Ranks,
		)
	}
	summary := livePlanSummary{
		Name:           migration.Name,
		Namespace:      migration.Namespace,
		Mode:           shardferryv1.MigrationModeLiveRankSplit,
		HandoffURL:     live.HandoffEndpoint,
		MaxInFlight:    live.MaxInFlight,
		StateMode:      live.StateMode,
		StateRoot:      live.StateRoot,
		Quorum:         live.Cutover.Quorum,
		RankPlan:       rankPlan,
		StateProviders: live.StateProviders,
	}
	if jsonOut {
		return writeJSON(out, summary)
	}
	fmt.Fprintf(out, "Name:           %s\n", summary.Name)
	fmt.Fprintf(out, "Namespace:      %s\n", summary.Namespace)
	fmt.Fprintf(out, "Mode:           %s\n", summary.Mode)
	fmt.Fprintf(out, "Handoff:        %s\n", emptyDash(summary.HandoffURL))
	fmt.Fprintf(out, "Max in flight:  %d\n", summary.MaxInFlight)
	fmt.Fprintf(out, "State mode:     %s\n", summary.StateMode)
	fmt.Fprintf(out, "State root:     %s\n", summary.StateRoot)
	fmt.Fprintf(out, "Quorum:         %d\n", summary.Quorum)
	if summary.RankPlan != "" {
		fmt.Fprintf(out, "Rank plan:      %s\n", summary.RankPlan)
	}
	if len(summary.StateProviders) > 0 {
		fmt.Fprintf(out, "State providers: %s\n", strings.Join(summary.StateProviders, ","))
	}
	fmt.Fprintln(out, "Prereq:         controller --live-url (token optional)")
	fmt.Fprintln(out, "Prereq:         executor must implement POST /v1/live/reconcile")
	if summary.StateMode == shardferryv1.LiveStateModeL4 {
		fmt.Fprintln(out, "Prereq:         target.validation is required for L4")
	}
	fmt.Fprintln(out, "Fallback:       none; switch to mode: static explicitly if live is unavailable")
	fmt.Fprintln(out, "Next:           shardferryctl preflight -f <file>; shardferryctl migrate -f <file> --wait")
	fmt.Fprintln(out, "Plan:           delegated to live executor (POST /v1/live/reconcile)")
	return nil
}

func writePlanSummary(out io.Writer, summary planSummary) error {
	fmt.Fprintf(out, "Name:              %s\n", summary.Name)
	fmt.Fprintf(out, "Namespace:         %s\n", summary.Namespace)
	fmt.Fprintf(out, "Source cluster:    %s\n", emptyDash(summary.SourceHost))
	fmt.Fprintf(out, "Target cluster:    %s\n", emptyDash(summary.TargetHost))
	fmt.Fprintf(out, "Source ranks:      %d\n", summary.SourceRanks)
	fmt.Fprintf(out, "Target ranks:      %d\n", summary.TargetRanks)
	fmt.Fprintf(out, "Plan hash:         %s\n", summary.PlanHash)
	fmt.Fprintf(out, "Bytes planned:     %d\n", summary.Bytes)
	fmt.Fprintf(out, "Theoretical min:   %d\n", summary.MinBytes)
	fmt.Fprintf(out, "Full gather:       %t\n", summary.FullGather)
	fmt.Fprintf(out, "Plan nodes:        %d\n", summary.NodeCount)
	fmt.Fprintf(out, "Range reads:       %d\n", summary.RangeReads)
	fmt.Fprintf(out, "Max fan-in:        %d\n", summary.MaxFanIn)
	if len(summary.TargetRoots) > 0 {
		fmt.Fprintf(out, "Target root[0]:    %s\n", summary.TargetRoots[0])
		fmt.Fprintf(out, "Target root[last]: %s\n", summary.TargetRoots[len(summary.TargetRoots)-1])
	}
	return nil
}

func int64Value(value any) int64 {
	switch typed := value.(type) {
	case int:
		return int64(typed)
	case int32:
		return int64(typed)
	case int64:
		return typed
	case float64:
		return int64(typed)
	default:
		return 0
	}
}

func boolValue(value any) bool {
	if typed, ok := value.(bool); ok {
		return typed
	}
	return false
}

func anySlice(value any) []any {
	if typed, ok := value.([]any); ok {
		return typed
	}
	return nil
}
