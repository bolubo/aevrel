package main

import (
	"context"
	"fmt"
	"io"
	"os"

	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/clusterclient"
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
	needsSourceProbe := migration.Spec.Source.AgentSelector != nil ||
		migration.Spec.Source.ClusterRef != ""
	needsTargetProbe := migration.Spec.Target.AgentSelector != nil ||
		migration.Spec.Target.ClusterRef != ""
	if token == "" && (needsSourceProbe || needsTargetProbe) {
		return fmt.Errorf("SHARDFERRY_AGENT_TOKEN is required for preflight")
	}
	agents := agentclient.New(token)
	resolver := &controller.AgentResolver{
		Client:   kubeClient,
		Agents:   agents,
		Clusters: clusterclient.New(""),
	}
	resolved, err := resolver.Resolve(ctx, migration)
	if err != nil {
		return fmt.Errorf("preflight: %w", err)
	}
	if needsSourceProbe {
		for _, entry := range resolved.Spec.Source.RankMap {
			inventory, err := agents.Inventory(ctx, entry.Endpoint, entry.Rank)
			if err != nil {
				return fmt.Errorf(
					"preflight source rank %d: %w",
					entry.Rank,
					err,
				)
			}
			if inventory.Rank != entry.Rank {
				return fmt.Errorf(
					"preflight source rank %d: agent returned rank %d",
					entry.Rank,
					inventory.Rank,
				)
			}
		}
	}
	if needsTargetProbe {
		checked := make(map[string]struct{})
		for _, entry := range resolved.Spec.Target.RankMap {
			if _, ok := checked[entry.Endpoint]; ok {
				continue
			}
			checked[entry.Endpoint] = struct{}{}
			if _, err := agents.Capabilities(ctx, entry.Endpoint); err != nil {
				return fmt.Errorf(
					"preflight target endpoint %s: %w",
					entry.Endpoint,
					err,
				)
			}
		}
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
