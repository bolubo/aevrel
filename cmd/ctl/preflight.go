package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
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
	clusterAPI := clusterclient.New("")
	tlsCA := os.Getenv("SHARDFERRY_TLS_CA")
	tlsCert := os.Getenv("SHARDFERRY_TLS_CERT")
	tlsKey := os.Getenv("SHARDFERRY_TLS_KEY")
	if tlsCA != "" || tlsCert != "" || tlsKey != "" {
		agents, err = agentclient.NewWithTLS(token, tlsCA, tlsCert, tlsKey)
		if err != nil {
			return fmt.Errorf("configure preflight agent TLS: %w", err)
		}
		clusterAPI, err = clusterclient.NewWithTLS("", tlsCA, tlsCert, tlsKey)
		if err != nil {
			return fmt.Errorf("configure preflight gateway TLS: %w", err)
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
		return fmt.Errorf("preflight: %w", err)
	}
	var sourceBytes int64
	if needsSourceProbe {
		orchestrator := &controller.Orchestrator{Agents: agents}
		if _, err := orchestrator.InstallSourceMetadata(ctx, resolved); err != nil {
			return fmt.Errorf("preflight source metadata: %w", err)
		}
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
			for _, chunk := range inventory.Chunks {
				for _, byteRange := range chunk.ByteRanges {
					if len(byteRange) == 2 && byteRange[1] > 0 {
						sourceBytes += byteRange[1]
					}
				}
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
			targetRoot := strings.ReplaceAll(
				migration.Spec.Target.RootTemplate,
				"{rank}",
				strconv.Itoa(int(entry.Rank)),
			)
			if _, err := agents.Preflight(
				ctx,
				entry.Endpoint,
				targetRoot,
				sourceBytes,
			); err != nil {
				return fmt.Errorf(
					"preflight target rank %d root %s: %w",
					entry.Rank,
					targetRoot,
					err,
				)
			}
		}
	}
	sourceRanks := len(resolved.Spec.Source.RankMap)
	targetRanks := len(resolved.Spec.Target.RankMap)
	sourceCluster := clusterStatus(
		ctx,
		kubeClient,
		migration.Namespace,
		migration.Spec.Source.ClusterRef,
	)
	targetCluster := clusterStatus(
		ctx,
		kubeClient,
		migration.Namespace,
		migration.Spec.Target.ClusterRef,
	)
	if jsonOut {
		return writeJSON(out, map[string]any{
			"status":       "ok",
			"name":         resolved.Name,
			"namespace":    resolved.Namespace,
			"source_ranks": sourceRanks,
			"target_ranks": targetRanks,
			"source":       resolved.Spec.Source.RankMap,
			"target":       resolved.Spec.Target.RankMap,
			"clusters": map[string]any{
				"source": sourceCluster,
				"target": targetCluster,
			},
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

func clusterStatus(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	ref string,
) map[string]any {
	if ref == "" {
		return map[string]any{"mode": "local"}
	}
	var cluster shardferryv1.ShardferryCluster
	err := kubeClient.Get(ctx, types.NamespacedName{
		Namespace: namespace,
		Name:      ref,
	}, &cluster)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return map[string]any{
				"name":    ref,
				"ready":   false,
				"message": "ShardferryCluster not found",
			}
		}
		return map[string]any{
			"name":    ref,
			"ready":   false,
			"message": err.Error(),
		}
	}
	return map[string]any{
		"name":     ref,
		"ready":    cluster.Status.Ready,
		"version":  cluster.Status.Version,
		"message":  cluster.Status.Message,
		"lastSeen": cluster.Status.LastSeen,
	}
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
