package main

import (
	"context"
	"fmt"
	"io"
	"os"

	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/controller"
)

func executePreflight(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	options, err := parsePreflight(args)
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
	if err := validateProductMigration(migration); err != nil {
		return fmt.Errorf("invalid migration: %w", err)
	}
	token := os.Getenv("SHARDFERRY_AGENT_TOKEN")
	if token == "" &&
		(migration.Spec.Source.AgentSelector != nil ||
			migration.Spec.Target.AgentSelector != nil) {
		return fmt.Errorf(
			"SHARDFERRY_AGENT_TOKEN is required for selector preflight",
		)
	}
	resolver := &controller.AgentResolver{
		Client: kubeClient,
		Agents: agentclient.New(token),
	}
	resolved, err := resolver.Resolve(ctx, migration)
	if err != nil {
		return fmt.Errorf("preflight: %w", err)
	}
	sourceRanks := len(resolved.Spec.Source.RankMap)
	targetRanks := len(resolved.Spec.Target.RankMap)
	if jsonOut {
		return writeJSON(out, map[string]any{
			"status":       "ok",
			"name":         resolved.Name,
			"namespace":    resolved.Namespace,
			"source_ranks": sourceRanks,
			"target_ranks": targetRanks,
			"source":       resolved.Spec.Source.RankMap,
			"target":       resolved.Spec.Target.RankMap,
		})
	}
	fmt.Fprintf(
		out,
		"preflight ok: %s/%s\nsource ranks: %d\ntarget ranks: %d\n",
		resolved.Namespace,
		resolved.Name,
		sourceRanks,
		targetRanks,
	)
	return nil
}

type preflightOptions struct {
	file string
}

func parsePreflight(args []string) (preflightOptions, error) {
	options := preflightOptions{}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "-f", "--file":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("%s requires a path", args[index-1])
			}
			options.file = args[index]
		case "-h", "--help":
			return options, fmt.Errorf(
				"usage: shardferryctl preflight -f migration.yaml",
			)
		default:
			return options, fmt.Errorf("unknown preflight option: %s", args[index])
		}
	}
	if options.file == "" {
		return options, fmt.Errorf("preflight requires -f migration.yaml")
	}
	return options, nil
}
