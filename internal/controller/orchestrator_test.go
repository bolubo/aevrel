package controller

import (
	"context"
	"strings"
	"testing"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/artifactstore"
)

type fakeAgentAPI struct {
	inventories  map[int32]agentclient.Inventory
	plan         map[string]any
	planned      bool
	metadata     []byte
	metaHash     string
	metaFetches  []string
	metaInstalls []string
}

type fakeTaskAPI struct {
	tasks     map[string]agentclient.Task
	committed []string
	failRank  int32
}

func (f *fakeTaskAPI) CreateTask(
	_ context.Context,
	_ string,
	task agentclient.Task,
) (agentclient.Task, error) {
	task.TaskID = "task-" + string(rune('0'+task.Rank))
	task.State = "created"
	f.tasks[task.TaskID] = task
	return task, nil
}

func (f *fakeTaskAPI) PrepareTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	task := f.tasks[taskID]
	task.State = "prepared"
	f.tasks[taskID] = task
	return task, nil
}

func (f *fakeTaskAPI) ExecuteTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	task := f.tasks[taskID]
	if task.Rank == f.failRank {
		task.State = "failed"
	} else {
		task.State = "staged"
	}
	f.tasks[taskID] = task
	return task, nil
}

func (f *fakeTaskAPI) CommitTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	task := f.tasks[taskID]
	task.State = "committed"
	f.tasks[taskID] = task
	f.committed = append(f.committed, taskID)
	return task, nil
}

func (f *fakeAgentAPI) Inventory(
	_ context.Context,
	_ string,
	rank int32,
) (agentclient.Inventory, error) {
	return f.inventories[rank], nil
}

func (f *fakeAgentAPI) Metadata(
	_ context.Context,
	endpoint string,
) ([]byte, string, error) {
	f.metaFetches = append(f.metaFetches, endpoint)
	if f.metaHash == "" {
		f.metaHash = "sha256:" + strings.Repeat("a", 64)
	}
	if f.metadata == nil {
		f.metadata = []byte("metadata")
	}
	return append([]byte(nil), f.metadata...), f.metaHash, nil
}

func (f *fakeAgentAPI) PutMetadata(
	_ context.Context,
	endpoint string,
	data []byte,
) (string, error) {
	f.metaInstalls = append(f.metaInstalls, endpoint)
	if f.metaHash == "" {
		f.metaHash = "sha256:" + strings.Repeat("a", 64)
	}
	return f.metaHash, nil
}

func (f *fakeAgentAPI) Plan(
	_ context.Context,
	_ string,
	spec map[string]any,
) (agentclient.PlanResponse, error) {
	f.planned = true
	return agentclient.PlanResponse{Version: "1", Plan: f.plan}, nil
}

func TestPrepareCollectsInventoryAndBuildsPlanSpec(t *testing.T) {
	migration := validMigration("orchestrate")
	migration.Spec.Source.RankMap[1].Endpoint = "https://source-1:9443"
	agents := &fakeAgentAPI{
		inventories: map[int32]agentclient.Inventory{
			0: fakeInventory(0),
			1: fakeInventory(1),
		},
		plan: map[string]any{"version": "1", "nodes": []any{}},
	}
	orchestrator := &Orchestrator{Agents: agents}

	prepared, err := orchestrator.Prepare(context.Background(), migration)
	if err != nil {
		t.Fatalf("prepare: %v", err)
	}
	if !agents.planned {
		t.Fatal("planner was not called")
	}
	if prepared.PlanHash == "" {
		t.Fatal("plan hash is empty")
	}
	source := prepared.Spec["source"].(map[string]any)
	shards := source["shards"].([]any)
	if len(shards) != 2 {
		t.Fatalf("shards = %d, want 2", len(shards))
	}
	tensors := prepared.Spec["checkpoint"].(map[string]any)["tensors"].([]any)
	if len(tensors) != 1 {
		t.Fatalf("tensors = %d, want 1", len(tensors))
	}
	if len(agents.metaFetches) != 1 {
		t.Fatalf("metadata fetches = %d, want 1", len(agents.metaFetches))
	}
	if len(agents.metaInstalls) != 2 {
		t.Fatalf("metadata installs = %d, want 2", len(agents.metaInstalls))
	}
}

func TestDispatchCommitsOnlyAfterAllRanksAreStaged(t *testing.T) {
	migration := validMigration("dispatch")
	tasks := &fakeTaskAPI{
		tasks:    map[string]agentclient.Task{},
		failRank: -1,
	}
	orchestrator := &Orchestrator{Tasks: tasks}
	prepared := Prepared{
		SpecRef: "https://controller/artifacts/spec",
		PlanRef: "https://controller/artifacts/plan",
		Plan: map[string]any{
			"version": "1",
			"nodes": []any{
				map[string]any{"target_rank": float64(0)},
				map[string]any{"target_rank": float64(1)},
			},
		},
	}

	result, err := orchestrator.Dispatch(context.Background(), migration, prepared)
	if err != nil {
		t.Fatalf("dispatch: %v", err)
	}
	if !result.Committed {
		t.Fatal("dispatch did not commit")
	}
	if len(tasks.committed) != 2 {
		t.Fatalf("committed tasks = %d, want 2", len(tasks.committed))
	}
}

func TestDispatchDoesNotCommitWhenOneRankFails(t *testing.T) {
	migration := validMigration("dispatch-fail")
	tasks := &fakeTaskAPI{
		tasks:    map[string]agentclient.Task{},
		failRank: 1,
	}
	orchestrator := &Orchestrator{Tasks: tasks}
	prepared := Prepared{
		SpecRef: "https://controller/artifacts/spec",
		PlanRef: "https://controller/artifacts/plan",
		Plan:    map[string]any{"version": "1", "nodes": []any{}},
	}

	result, err := orchestrator.Dispatch(context.Background(), migration, prepared)
	if err == nil {
		t.Fatal("expected dispatch failure")
	}
	if result.Committed || len(tasks.committed) != 0 {
		t.Fatal("failed rank must block all commits")
	}
}

func TestPreparePublishesSpecAndPlanArtifacts(t *testing.T) {
	migration := validMigration("artifacts")
	agents := &fakeAgentAPI{
		inventories: map[int32]agentclient.Inventory{
			0: fakeInventory(0),
			1: fakeInventory(1),
		},
		plan: map[string]any{"version": "1", "nodes": []any{}},
	}
	store := artifactstore.NewMemoryStore("http://controller:8082")
	orchestrator := &Orchestrator{
		Agents:    agents,
		Artifacts: store,
	}
	prepared, err := orchestrator.Prepare(context.Background(), migration)
	if err != nil {
		t.Fatalf("prepare: %v", err)
	}
	if prepared.SpecRef == "" || prepared.PlanRef == "" {
		t.Fatalf("missing artifact refs: %+v", prepared)
	}
	if _, err := store.Get(context.Background(), prepared.PlanRef); err != nil {
		t.Fatalf("read plan artifact: %v", err)
	}
}

func fakeInventory(rank int32) agentclient.Inventory {
	return agentclient.Inventory{
		Version:      "1",
		Rank:         rank,
		Format:       "dcp",
		MetadataHash: "sha256:" + strings.Repeat("a", 64),
		Tensors: []agentclient.Tensor{{
			Name: "model.weight", Shape: []int32{8, 4}, Dtype: "float32",
		}},
		Chunks: []agentclient.Chunk{{
			Tensor:       "model.weight",
			Rank:         rank,
			File:         "https://source/shard",
			TensorOffset: []int32{rank * 4, 0},
			TensorSize:   []int32{4, 4},
			ByteRanges:   [][]int64{{0, 64}},
			Checksum:     "sha256:" + strings.Repeat("b", 64),
		}},
		AgentVersion: "1.0.0",
		TorchVersion: "2.14.1",
	}
}
