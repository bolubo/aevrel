package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"reflect"
	"sort"
	"strconv"
	"strings"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/artifactstore"
)

type AgentAPI interface {
	Metadata(context.Context, string) ([]byte, string, error)
	PutMetadata(context.Context, string, []byte) (string, error)
	Inventory(context.Context, string, int32) (agentclient.Inventory, error)
	Plan(context.Context, string, map[string]any) (agentclient.PlanResponse, error)
}

type TaskAPI interface {
	CreateTask(context.Context, string, agentclient.Task) (agentclient.Task, error)
	PrepareTask(context.Context, string, string) (agentclient.Task, error)
	ExecuteTask(context.Context, string, string) (agentclient.Task, error)
	CommitTask(context.Context, string, string) (agentclient.Task, error)
}

type ProofAPI interface {
	MergeProof(context.Context, string, []map[string]any) (map[string]any, error)
}

type Orchestrator struct {
	Agents          AgentAPI
	Tasks           TaskAPI
	Proofs          ProofAPI
	Artifacts       artifactstore.Store
	PlannerEndpoint string
}

type Prepared struct {
	Spec     map[string]any
	Plan     map[string]any
	PlanHash string
	SpecRef  string
	PlanRef  string
}

type DispatchResult struct {
	Tasks     []agentclient.Task
	Committed bool
	Reports   []map[string]any
	ReportRef string
	ProofRef  string
	ProofHash string
}

func (o *Orchestrator) Prepare(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (Prepared, error) {
	if o.Agents == nil {
		return Prepared{}, fmt.Errorf("agent client is not configured")
	}
	if err := validateMigration(migration); err != nil {
		return Prepared{}, err
	}
	endpoints := append([]aevrelv1.RankEndpoint(nil), migration.Spec.Source.RankMap...)
	sort.Slice(endpoints, func(i, j int) bool {
		return endpoints[i].Rank < endpoints[j].Rank
	})
	if len(endpoints) == 0 {
		return Prepared{}, fmt.Errorf("source rankMap is empty")
	}
	metadataHash, err := o.InstallSourceMetadata(ctx, migration)
	if err != nil {
		return Prepared{}, err
	}
	inventories := make([]agentclient.Inventory, 0, len(endpoints))
	for _, endpoint := range endpoints {
		inventory, err := o.Agents.Inventory(ctx, endpoint.Endpoint, endpoint.Rank)
		if err != nil {
			return Prepared{}, fmt.Errorf(
				"inventory rank %d: %w",
				endpoint.Rank,
				err,
			)
		}
		if inventory.Rank != endpoint.Rank {
			return Prepared{}, fmt.Errorf(
				"inventory rank mismatch: requested %d, got %d",
				endpoint.Rank,
				inventory.Rank,
			)
		}
		if metadataHash != "" && inventory.MetadataHash != metadataHash {
			return Prepared{}, fmt.Errorf(
				"inventory rank %d metadata hash %s != owner %s",
				endpoint.Rank,
				inventory.MetadataHash,
				metadataHash,
			)
		}
		inventories = append(inventories, inventory)
	}
	spec, err := buildMigrationSpec(migration, inventories)
	if err != nil {
		return Prepared{}, err
	}
	plannerEndpoint := o.PlannerEndpoint
	if plannerEndpoint == "" {
		plannerEndpoint = endpoints[0].Endpoint
	}
	response, err := o.Agents.Plan(ctx, plannerEndpoint, spec)
	if err != nil {
		return Prepared{}, fmt.Errorf("plan: %w", err)
	}
	if response.Plan == nil {
		return Prepared{}, fmt.Errorf("planner returned an empty plan")
	}
	hash, err := planHash(response.Plan)
	if err != nil {
		return Prepared{}, err
	}
	prepared := Prepared{
		Spec:     spec,
		Plan:     response.Plan,
		PlanHash: hash,
	}
	if o.Artifacts != nil {
		specBytes, err := json.Marshal(spec)
		if err != nil {
			return Prepared{}, fmt.Errorf("encode spec artifact: %w", err)
		}
		specRef, err := o.Artifacts.Put(ctx, specBytes)
		if err != nil {
			return Prepared{}, fmt.Errorf("publish spec artifact: %w", err)
		}
		planBytes, err := json.Marshal(response.Plan)
		if err != nil {
			return Prepared{}, fmt.Errorf("encode plan artifact: %w", err)
		}
		planRef, err := o.Artifacts.Put(ctx, planBytes)
		if err != nil {
			return Prepared{}, fmt.Errorf("publish plan artifact: %w", err)
		}
		prepared.SpecRef = specRef.URL
		prepared.PlanRef = planRef.URL
	}
	return prepared, nil
}

// InstallSourceMetadata fetches the small DCP metadata file from the declared
// owner rank and installs it on every unique source agent endpoint.
func (o *Orchestrator) InstallSourceMetadata(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (string, error) {
	if o.Agents == nil {
		return "", fmt.Errorf("agent client is not configured")
	}
	ownerRank := migration.Spec.Source.MetadataOwnerRank
	ownerEndpoint := ""
	for _, endpoint := range migration.Spec.Source.RankMap {
		if endpoint.Rank == ownerRank {
			ownerEndpoint = endpoint.Endpoint
			break
		}
	}
	if ownerEndpoint == "" {
		return "", fmt.Errorf(
			"source metadata owner rank %d is not in rankMap",
			ownerRank,
		)
	}
	data, digest, err := o.Agents.Metadata(ctx, ownerEndpoint)
	if err != nil {
		return "", fmt.Errorf(
			"fetch source metadata from rank %d: %w",
			ownerRank,
			err,
		)
	}
	seen := make(map[string]struct{})
	for _, endpoint := range migration.Spec.Source.RankMap {
		if _, exists := seen[endpoint.Endpoint]; exists {
			continue
		}
		seen[endpoint.Endpoint] = struct{}{}
		installed, err := o.Agents.PutMetadata(
			ctx,
			endpoint.Endpoint,
			data,
		)
		if err != nil {
			return "", fmt.Errorf(
				"install source metadata on %s: %w",
				endpoint.Endpoint,
				err,
			)
		}
		if installed != digest {
			return "", fmt.Errorf(
				"source agent %s installed metadata %s, want %s",
				endpoint.Endpoint,
				installed,
				digest,
			)
		}
	}
	return digest, nil
}

func (o *Orchestrator) Dispatch(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	prepared Prepared,
) (DispatchResult, error) {
	if o.Tasks == nil {
		return DispatchResult{}, fmt.Errorf("task client is not configured")
	}
	if prepared.SpecRef == "" || prepared.PlanRef == "" {
		return DispatchResult{}, fmt.Errorf("spec/plan artifact refs are required")
	}
	endpoints := append(
		[]aevrelv1.RankEndpoint(nil),
		migration.Spec.Target.RankMap...,
	)
	sort.Slice(endpoints, func(i, j int) bool {
		return endpoints[i].Rank < endpoints[j].Rank
	})
	if len(endpoints) == 0 {
		return DispatchResult{}, fmt.Errorf("target rankMap is empty")
	}
	items := make([]dispatchItem, 0, len(endpoints))
	for _, endpoint := range endpoints {
		targetRoot := strings.ReplaceAll(
			migration.Spec.Target.RootTemplate,
			"{rank}",
			strconv.Itoa(int(endpoint.Rank)),
		)
		task := agentclient.Task{
			Version:      "1",
			MigrationUID: migrationUID(migration),
			CommitToken:  migrationUID(migration),
			Rank:         endpoint.Rank,
			TargetRoot:   targetRoot,
			SpecRef:      prepared.SpecRef,
			PlanRef:      prepared.PlanRef,
			PlanSlice:    planSlice(prepared.Plan, endpoint.Rank),
			State:        "created",
		}
		task.TaskHash = taskHash(task)
		created, err := o.Tasks.CreateTask(ctx, endpoint.Endpoint, task)
		if err != nil {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"create rank %d task: %w",
				endpoint.Rank,
				err,
			)
		}
		items = append(items, dispatchItem{
			endpoint: endpoint.Endpoint,
			task:     created,
		})
	}
	for index := range items {
		task, err := o.Tasks.PrepareTask(
			ctx,
			items[index].endpoint,
			items[index].task.TaskID,
		)
		if err != nil {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"prepare rank %d: %w",
				items[index].task.Rank,
				err,
			)
		}
		items[index].task = task
		if task.State != "prepared" {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"rank %d did not prepare: %s",
				task.Rank,
				task.State,
			)
		}
	}
	for index := range items {
		task, err := o.Tasks.ExecuteTask(
			ctx,
			items[index].endpoint,
			items[index].task.TaskID,
		)
		if err != nil {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"execute rank %d: %w",
				items[index].task.Rank,
				err,
			)
		}
		items[index].task = task
		if task.State != "staged" {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"rank %d did not stage: %s",
				task.Rank,
				task.State,
			)
		}
	}
	for index := range items {
		task, err := o.Tasks.CommitTask(
			ctx,
			items[index].endpoint,
			items[index].task.TaskID,
		)
		if err != nil {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"commit rank %d: %w",
				items[index].task.Rank,
				err,
			)
		}
		items[index].task = task
		if task.State != "committed" {
			return DispatchResult{Tasks: collectTasks(items)}, fmt.Errorf(
				"rank %d did not commit: %s",
				task.Rank,
				task.State,
			)
		}
	}
	tasks := collectTasks(items)
	reports := make([]map[string]any, 0, len(tasks))
	for _, task := range tasks {
		if task.Report != nil {
			reports = append(reports, map[string]any{
				"rank":   task.Rank,
				"report": task.Report,
			})
		}
	}
	result := DispatchResult{
		Tasks:     tasks,
		Committed: true,
		Reports:   reports,
	}
	if o.Artifacts != nil {
		if o.Proofs != nil {
			rankProofs := make([]map[string]any, 0)
			for _, report := range reports {
				row, _ := report["report"].(map[string]any)
				if proof, ok := row["proof"].(map[string]any); ok {
					rankProofs = append(rankProofs, proof)
				}
			}
			if len(rankProofs) > 0 {
				if len(rankProofs) != len(reports) {
					return result, fmt.Errorf(
						"proof reports are incomplete: %d of %d",
						len(rankProofs),
						len(reports),
					)
				}
				endpoint := ""
				if len(endpoints) > 0 {
					endpoint = endpoints[0].Endpoint
				}
				proof, err := o.Proofs.MergeProof(ctx, endpoint, rankProofs)
				if err != nil {
					return result, fmt.Errorf("merge migration proof: %w", err)
				}
				root, _ := proof["root"].(string)
				if root == "" {
					return result, fmt.Errorf("merged proof has no root")
				}
				payload, err := json.Marshal(proof)
				if err != nil {
					return result, fmt.Errorf("encode migration proof: %w", err)
				}
				ref, err := o.Artifacts.Put(ctx, payload)
				if err != nil {
					return result, fmt.Errorf("publish migration proof: %w", err)
				}
				result.ProofRef = ref.URL
				result.ProofHash = root
			}
		}
		reports = attachDispatchProofRefs(reports, result.ProofRef, result.ProofHash)
		payload, err := json.Marshal(map[string]any{
			"version":       "1",
			"migration_uid": migrationUID(migration),
			"reports":       reports,
		})
		if err != nil {
			return result, fmt.Errorf("encode report artifact: %w", err)
		}
		ref, err := o.Artifacts.Put(ctx, payload)
		if err != nil {
			return result, fmt.Errorf("publish report artifact: %w", err)
		}
		result.ReportRef = ref.URL
	}
	return result, nil
}

func attachDispatchProofRefs(
	reports []map[string]any,
	proofRef string,
	proofHash string,
) []map[string]any {
	if proofRef == "" || proofHash == "" {
		return reports
	}
	result := make([]map[string]any, len(reports))
	for index, item := range reports {
		copyItem := make(map[string]any, len(item))
		for key, value := range item {
			copyItem[key] = value
		}
		if report, ok := item["report"].(map[string]any); ok {
			reportCopy := make(map[string]any, len(report))
			for key, value := range report {
				reportCopy[key] = value
			}
			if reportCopy["proof"] != nil {
				reportCopy["proof_ref"] = proofRef
				reportCopy["proof_hash"] = proofHash
			}
			copyItem["report"] = reportCopy
		}
		result[index] = copyItem
	}
	return result
}

type dispatchItem struct {
	endpoint string
	task     agentclient.Task
}

func collectTasks(items []dispatchItem) []agentclient.Task {
	tasks := make([]agentclient.Task, 0, len(items))
	for _, item := range items {
		tasks = append(tasks, item.task)
	}
	return tasks
}

func migrationUID(migration *aevrelv1.CheckpointMigration) string {
	if migration.UID != "" {
		return string(migration.UID)
	}
	return migration.Namespace + "/" + migration.Name
}

func taskHash(task agentclient.Task) string {
	data, _ := json.Marshal(map[string]any{
		"migration_uid": task.MigrationUID,
		"commit_token":  task.CommitToken,
		"rank":          task.Rank,
		"target_root":   task.TargetRoot,
		"spec_ref":      task.SpecRef,
		"plan_ref":      task.PlanRef,
	})
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func planSlice(plan map[string]any, rank int32) map[string]any {
	nodes, _ := plan["nodes"].([]any)
	filtered := make([]any, 0)
	for _, node := range nodes {
		row, ok := node.(map[string]any)
		if !ok {
			continue
		}
		if int32Value(row["target_rank"]) == rank {
			filtered = append(filtered, row)
		}
	}
	return map[string]any{"nodes": filtered, "bytes": 0}
}

func int32Value(value any) int32 {
	switch typed := value.(type) {
	case int:
		return int32(typed)
	case int32:
		return typed
	case float64:
		return int32(typed)
	case json.Number:
		value, _ := typed.Int64()
		return int32(value)
	default:
		return -1
	}
}

func buildMigrationSpec(
	migration *aevrelv1.CheckpointMigration,
	inventories []agentclient.Inventory,
) (map[string]any, error) {
	if len(inventories) == 0 {
		return nil, fmt.Errorf("no source inventories")
	}
	first := inventories[0]
	if first.Format != "dcp" {
		return nil, fmt.Errorf("unsupported inventory format: %s", first.Format)
	}
	if first.MetadataHash == "" {
		return nil, fmt.Errorf("inventory metadata hash is empty")
	}
	if len(first.Tensors) == 0 {
		return nil, fmt.Errorf("inventory has no tensors")
	}
	tensors := make([]any, 0, len(first.Tensors))
	for _, tensor := range first.Tensors {
		if tensor.Name == "" || len(tensor.Shape) == 0 || tensor.Dtype == "" {
			return nil, fmt.Errorf("inventory contains an incomplete tensor")
		}
		tensors = append(tensors, map[string]any{
			"name":  tensor.Name,
			"shape": tensor.Shape,
			"dtype": tensor.Dtype,
		})
	}
	shards := make([]any, 0)
	for _, inventory := range inventories {
		if inventory.Format != first.Format {
			return nil, fmt.Errorf("inventories use different formats")
		}
		if inventory.MetadataHash != first.MetadataHash {
			return nil, fmt.Errorf("inventories use different metadata hashes")
		}
		if !reflect.DeepEqual(inventory.Tensors, first.Tensors) {
			return nil, fmt.Errorf("inventories use different tensor schemas")
		}
		for _, chunk := range inventory.Chunks {
			if chunk.File == "" || chunk.Checksum == "" {
				return nil, fmt.Errorf("inventory contains an incomplete chunk")
			}
			shard := map[string]any{
				"tensor":   chunk.Tensor,
				"rank":     chunk.Rank,
				"uri":      chunk.File,
				"offset":   chunk.TensorOffset,
				"size":     chunk.TensorSize,
				"checksum": chunk.Checksum,
			}
			if chunk.ID != "" {
				shard["id"] = chunk.ID
			}
			shards = append(shards, shard)
		}
	}
	sourceMesh := meshPayload(migration.Spec.Source.WorldSize, migration.Spec.Source.Mesh)
	targetMesh := meshPayload(migration.Spec.Target.WorldSize, migration.Spec.Target.Mesh)
	return map[string]any{
		"version": "1",
		"checkpoint": map[string]any{
			"format":         migration.Spec.Checkpoint.Format,
			"format_version": migration.Spec.Checkpoint.FormatVersion,
			"tensors":        tensors,
		},
		"source": map[string]any{
			"mesh":    sourceMesh,
			"storage": map[string]any{"uri": "served"},
			"shards":  shards,
		},
		"target": map[string]any{
			"mesh":    targetMesh,
			"storage": map[string]any{"uri": "target"},
		},
	}, nil
}

func meshPayload(worldSize int32, mesh aevrelv1.MeshSpec) map[string]any {
	return map[string]any{
		"shape":      mesh.Shape,
		"world_size": worldSize,
		"placements": mesh.Placements,
	}
}

func planHash(plan map[string]any) (string, error) {
	data, err := json.Marshal(plan)
	if err != nil {
		return "", fmt.Errorf("hash plan: %w", err)
	}
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}
