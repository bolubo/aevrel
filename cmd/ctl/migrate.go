package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

const defaultArtifactClaim = "shardferry-artifacts"

type migrateOptions struct {
	file          string
	wait          bool
	dryRun        bool
	timeout       time.Duration
	artifactClaim string
}

func executeMigrate(
	ctx context.Context,
	kubeClient client.Client,
	namespace string,
	jsonOut bool,
	args []string,
	out io.Writer,
) error {
	options, err := parseMigrate(args)
	if err != nil {
		return err
	}
	migration, err := loadMigration(options.file, namespace)
	if err != nil {
		return err
	}
	if options.artifactClaim != "" {
		migration.Spec.Artifacts.ClaimName = options.artifactClaim
	}
	if !containsFinalizer(migration, "shardferry.io/cleanup") {
		migration.Finalizers = append(
			migration.Finalizers,
			"shardferry.io/cleanup",
		)
	}
	if err := validateProductMigration(migration); err != nil {
		return fmt.Errorf("invalid migration: %w", err)
	}
	if options.dryRun {
		document, err := migrationDocument(migration)
		if err != nil {
			return err
		}
		if jsonOut {
			return writeJSON(out, document)
		}
		data, err := yaml.Marshal(document)
		if err != nil {
			return err
		}
		_, err = out.Write(data)
		return err
	}
	if kubeClient == nil {
		return fmt.Errorf("Kubernetes client is not configured")
	}
	if err := kubeClient.Create(ctx, migration); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return fmt.Errorf(
				"CheckpointMigration %s/%s already exists; use another metadata.name or delete it first",
				migration.Namespace,
				migration.Name,
			)
		}
		return err
	}
	if !options.wait {
		if jsonOut {
			return writeJSON(out, migration)
		}
		fmt.Fprintf(
			out,
			"created migration %s/%s\nnext: shardferryctl describe migration %s -n %s\n",
			migration.Namespace,
			migration.Name,
			migration.Name,
			migration.Namespace,
		)
		return nil
	}
	return waitForMigration(ctx, kubeClient, migration, options, jsonOut, out)
}

func parseMigrate(args []string) (migrateOptions, error) {
	options := migrateOptions{timeout: time.Hour}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "-f", "--file":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("%s requires a path", args[index-1])
			}
			options.file = args[index]
		case "--wait":
			options.wait = true
		case "--dry-run":
			options.dryRun = true
		case "--timeout":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--timeout requires a duration")
			}
			value, err := time.ParseDuration(args[index])
			if err != nil || value <= 0 {
				return options, fmt.Errorf("invalid --timeout: %s", args[index])
			}
			options.timeout = value
		case "--artifact-claim":
			index++
			if index >= len(args) {
				return options, fmt.Errorf("--artifact-claim requires a name")
			}
			options.artifactClaim = args[index]
		case "-h", "--help":
			return options, fmt.Errorf(
				"usage: shardferryctl migrate -f migration.yaml [--wait] [--dry-run] [--timeout 1h] [--artifact-claim NAME]",
			)
		default:
			return options, fmt.Errorf("unknown migrate option: %s", args[index])
		}
	}
	if options.file == "" {
		return options, fmt.Errorf("migrate requires -f migration.yaml")
	}
	return options, nil
}

func loadMigration(path, namespace string) (*shardferryv1.CheckpointMigration, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var migration shardferryv1.CheckpointMigration
	if err := yaml.Unmarshal(data, &migration); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if migration.APIVersion == "" {
		migration.APIVersion = "shardferry.io/v1"
	}
	if migration.Kind == "" {
		migration.Kind = "CheckpointMigration"
	}
	if migration.APIVersion != "shardferry.io/v1" ||
		migration.Kind != "CheckpointMigration" {
		return nil, fmt.Errorf(
			"expected shardferry.io/v1 CheckpointMigration, got %s %s",
			migration.APIVersion,
			migration.Kind,
		)
	}
	if migration.Name == "" {
		return nil, fmt.Errorf("metadata.name is required")
	}
	if migration.Namespace == "" {
		migration.Namespace = namespace
	}
	applyMigrationDefaults(&migration)
	return &migration, nil
}

func migrationDocument(
	migration *shardferryv1.CheckpointMigration,
) (map[string]any, error) {
	document, err := runtime.DefaultUnstructuredConverter.ToUnstructured(migration)
	if err != nil {
		return nil, err
	}
	delete(document, "status")
	return document, nil
}

func applyMigrationDefaults(migration *shardferryv1.CheckpointMigration) {
	spec := &migration.Spec
	if spec.Checkpoint.Format == "" {
		spec.Checkpoint.Format = "dcp"
	}
	if spec.Checkpoint.FormatVersion == "" {
		spec.Checkpoint.FormatVersion = "1.0.0"
	}
	if len(spec.Source.Mesh.Shape) == 0 {
		spec.Source.Mesh.Shape = []int32{spec.Source.WorldSize}
	}
	if len(spec.Source.Mesh.Placements) == 0 {
		spec.Source.Mesh.Placements = []string{"Shard(0)"}
	}
	if len(spec.Target.Mesh.Shape) == 0 {
		spec.Target.Mesh.Shape = []int32{spec.Target.WorldSize}
	}
	if len(spec.Target.Mesh.Placements) == 0 {
		spec.Target.Mesh.Placements = []string{"Shard(0)"}
	}
	if spec.Policy.Verify == "" {
		spec.Policy.Verify = "bitwise"
	}
	if spec.Policy.Commit == "" {
		spec.Policy.Commit = "all-or-nothing"
	}
	if spec.Policy.MaxConcurrentTransfers == 0 {
		spec.Policy.MaxConcurrentTransfers = 16
	}
	if spec.Policy.MaxAttemptsPerRank == 0 {
		spec.Policy.MaxAttemptsPerRank = 5
	}
	if spec.Policy.TimeoutSeconds == 0 {
		spec.Policy.TimeoutSeconds = 3600
	}
	if spec.Policy.SourceChunkCacheBytes == 0 {
		spec.Policy.SourceChunkCacheBytes = 2 * 1024 * 1024 * 1024
	}
	if spec.Artifacts.Store == "" {
		spec.Artifacts.Store = "pvc"
	}
	if spec.Artifacts.ClaimName == "" && spec.Artifacts.Store == "pvc" {
		spec.Artifacts.ClaimName = defaultArtifactClaim
	}
}

func validateProductMigration(
	migration *shardferryv1.CheckpointMigration,
) error {
	spec := migration.Spec
	if spec.Checkpoint.Format != "dcp" {
		return fmt.Errorf("checkpoint.format must be dcp")
	}
	if spec.Source.WorldSize < 1 || spec.Source.WorldSize > 256 {
		return fmt.Errorf("source.worldSize is out of range")
	}
	if spec.Target.WorldSize < 1 || spec.Target.WorldSize > 256 {
		return fmt.Errorf("target.worldSize is out of range")
	}
	if err := validateAgentConfig(
		"source",
		spec.Source.RankMap,
		spec.Source.AgentSelector,
		spec.Source.ClusterRef,
	); err != nil {
		return err
	}
	if err := validateAgentConfig(
		"target",
		spec.Target.RankMap,
		spec.Target.AgentSelector,
		spec.Target.ClusterRef,
	); err != nil {
		return err
	}
	if spec.Source.CheckpointRoot == "" {
		return fmt.Errorf("source.checkpointRoot is required")
	}
	if spec.Target.RootTemplate == "" {
		return fmt.Errorf("target.rootTemplate is required")
	}
	if spec.Policy.Verify != "bitwise" {
		return fmt.Errorf("policy.verify must be bitwise")
	}
	if spec.Policy.Commit != "all-or-nothing" {
		return fmt.Errorf("policy.commit must be all-or-nothing")
	}
	if spec.Policy.MaxConcurrentTransfers < 1 ||
		spec.Policy.MaxAttemptsPerRank < 1 ||
		spec.Policy.TimeoutSeconds < 1 ||
		spec.Policy.SourceChunkCacheBytes < 1 {
		return fmt.Errorf("policy limits must be positive")
	}
	if spec.Artifacts.Store != "pvc" {
		return fmt.Errorf("only pvc artifact store is supported by this CLI")
	}
	if spec.Artifacts.ClaimName == "" {
		return fmt.Errorf("artifacts.claimName is required")
	}
	return nil
}

func validateAgentConfig(
	role string,
	rankMap []shardferryv1.RankEndpoint,
	selector *shardferryv1.AgentSelector,
	clusterRef string,
) error {
	if clusterRef != "" {
		if len(rankMap) > 0 {
			return fmt.Errorf(
				"%s must not set rankMap when clusterRef is set",
				role,
			)
		}
		if selector == nil || len(selector.MatchLabels) == 0 {
			return fmt.Errorf(
				"%s.agentSelector.matchLabels is required with clusterRef",
				role,
			)
		}
		if selector.Port < 0 || selector.Port > 65535 {
			return fmt.Errorf("%s.agentSelector.port is out of range", role)
		}
		switch selector.Scheme {
		case "", "http", "https":
		default:
			return fmt.Errorf("%s.agentSelector.scheme must be http or https", role)
		}
		return nil
	}
	if len(rankMap) > 0 && selector != nil {
		return fmt.Errorf(
			"%s must set either rankMap or agentSelector, not both",
			role,
		)
	}
	if len(rankMap) == 0 && selector == nil {
		return fmt.Errorf("%s.rankMap or agentSelector is required", role)
	}
	if selector != nil && len(selector.MatchLabels) == 0 {
		return fmt.Errorf("%s.agentSelector.matchLabels is required", role)
	}
	return nil
}

func waitForMigration(
	ctx context.Context,
	kubeClient client.Client,
	migration *shardferryv1.CheckpointMigration,
	options migrateOptions,
	jsonOut bool,
	out io.Writer,
) error {
	waitCtx, cancel := context.WithTimeout(ctx, options.timeout)
	defer cancel()
	key := client.ObjectKeyFromObject(migration)
	lastPhase := ""
	for {
		var current shardferryv1.CheckpointMigration
		if err := kubeClient.Get(waitCtx, key, &current); err != nil {
			return err
		}
		if !jsonOut && current.Status.Phase != lastPhase {
			fmt.Fprintf(
				out,
				"phase=%s prepared=%d staged=%d committed=%d failed=%d\n",
				current.Status.Phase,
				current.Status.Counts.Prepared,
				current.Status.Counts.Staged,
				current.Status.Counts.Committed,
				current.Status.Counts.Failed,
			)
			lastPhase = current.Status.Phase
		}
		switch current.Status.Phase {
		case shardferryv1.MigrationPhaseSucceeded:
			if jsonOut {
				return writeJSON(out, &current)
			}
			fmt.Fprintf(
				out,
				"migration succeeded: %s/%s report=%s\n",
				current.Namespace,
				current.Name,
				emptyDash(current.Status.ReportRef),
			)
			return nil
		case shardferryv1.MigrationPhaseFailed,
			shardferryv1.MigrationPhaseCancelled,
			shardferryv1.MigrationPhaseCommitIncomplete:
			if jsonOut {
				if err := writeJSON(out, &current); err != nil {
					return err
				}
			}
			return fmt.Errorf(
				"migration ended in phase %s: %s",
				current.Status.Phase,
				conditionMessage(&current, "Accepted"),
			)
		}
		select {
		case <-waitCtx.Done():
			return fmt.Errorf(
				"timed out after %s waiting for migration %s/%s",
				options.timeout,
				current.Namespace,
				current.Name,
			)
		case <-time.After(2 * time.Second):
		}
	}
}

func conditionMessage(
	migration *shardferryv1.CheckpointMigration,
	conditionType string,
) string {
	for _, condition := range migration.Status.Conditions {
		if condition.Type == conditionType {
			return condition.Message
		}
	}
	return ""
}
