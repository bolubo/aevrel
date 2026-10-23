package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/artifactstore"
)

type fakeTaskClient struct {
	mu                      sync.Mutex
	tasks                   map[string]agentclient.Task
	run                     *Run
	commitCount             int
	failExecuteRank         int32
	commitWithoutIntentSeen bool
	blockCreate             bool
	withProof               bool
	omitCommitReport        bool
	executeConflict         bool
	getState                string
}

func newFakeTaskClient(run *Run) *fakeTaskClient {
	return &fakeTaskClient{
		tasks:           make(map[string]agentclient.Task),
		run:             run,
		failExecuteRank: -1,
	}
}

func (f *fakeTaskClient) CreateTask(
	ctx context.Context,
	_ string,
	task agentclient.Task,
) (agentclient.Task, error) {
	if f.blockCreate {
		<-ctx.Done()
		return agentclient.Task{}, ctx.Err()
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	key := fmt.Sprintf("task-%d", task.Rank)
	task.TaskID = key
	task.State = "created"
	f.tasks[key] = task
	return task, nil
}

func (f *fakeTaskClient) PrepareTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	task.State = "prepared"
	f.tasks[taskID] = task
	return task, nil
}

func (f *fakeTaskClient) ExecuteTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.executeConflict {
		return agentclient.Task{}, &agentclient.AgentError{
			StatusCode: 409,
			Code:       "TARGET_CONFLICT",
			Details:    "cannot be executed from commit-incomplete",
		}
	}
	task := f.tasks[taskID]
	if task.Rank == f.failExecuteRank {
		task.State = "failed"
	} else {
		task.State = "staged"
	}
	f.tasks[taskID] = task
	return task, nil
}

func (f *fakeTaskClient) CommitTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.run.HasCommitIntent() {
		f.commitWithoutIntentSeen = true
	}
	task := f.tasks[taskID]
	task.State = "committed"
	if !f.omitCommitReport {
		task.Report = f.report(true)
	}
	f.tasks[taskID] = task
	f.commitCount++
	return task, nil
}

func (f *fakeTaskClient) GetTask(
	_ context.Context,
	_ string,
	taskID string,
) (agentclient.Task, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	task := f.tasks[taskID]
	if f.getState != "" {
		task.State = f.getState
	}
	if task.Report == nil {
		task.Report = f.report(false)
	}
	f.tasks[taskID] = task
	return task, nil
}

func (f *fakeTaskClient) report(withProof bool) map[string]any {
	report := map[string]any{"status": "success"}
	if withProof && f.withProof {
		report["proof"] = map[string]any{
			"plan_id": "plan",
			"root":    "sha256:" + strings.Repeat("a", 64),
		}
	}
	return report
}

func newTestRun(world int32) *Run {
	config := RunConfig{
		ID:            "run-128",
		PlanID:        "sha256:" + strings.Repeat("a", 64),
		MigrationUID:  "m-128",
		TargetWorld:   world,
		SpecRef:       "http://artifact/spec",
		PlanRef:       "http://artifact/plan",
		RootTemplate:  "/target/rank{rank}",
		Plan:          map[string]any{"nodes": []any{}},
		Endpoints:     make(map[int32]string, world),
		MaxAttempts:   1,
		RequireReport: true,
	}
	run := NewRun(config)
	for rank := int32(0); rank < world; rank++ {
		run.RegisterRank(rank)
		config.Endpoints[rank] = fmt.Sprintf("http://agent-%d:9443", rank)
	}
	return run
}

func TestRunnerCommits128Ranks(t *testing.T) {
	run := newTestRun(128)
	client := newFakeTaskClient(run)
	runner := NewRunner(client, 16)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if got := run.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("phase = %s, want succeeded", got)
	}
	if client.commitCount != 128 {
		t.Fatalf("commits = %d, want 128", client.commitCount)
	}
	if client.commitWithoutIntentSeen {
		t.Fatal("commit happened before CommitIntent")
	}
}

func TestRunnerRecoversCommitIncompleteTaskAfterRestart(t *testing.T) {
	run := newTestRun(1)
	run.SetPhase(0, PhasePrepared, "task-0", "")
	client := newFakeTaskClient(run)
	client.executeConflict = true
	client.getState = "commit-incomplete"
	client.tasks["task-0"] = agentclient.Task{
		TaskID: "task-0",
		State:  "commit-incomplete",
	}
	runner := NewRunner(client, 1)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if got := run.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("phase = %s, want succeeded", got)
	}
	if !run.HasCommitIntent() {
		t.Fatal("recovered commit-incomplete task did not restore commit intent")
	}
	if client.commitWithoutIntentSeen {
		t.Fatal("commit request happened before commit intent was restored")
	}
}

func TestRunnerRecoversCommittedTaskReportAfterRestart(t *testing.T) {
	run := newTestRun(1)
	run.SetPhase(0, PhaseCommitted, "task-0", "")
	client := newFakeTaskClient(run)
	client.getState = "committed"
	client.tasks["task-0"] = agentclient.Task{
		TaskID: "task-0",
		State:  "committed",
	}
	store := artifactstore.NewMemoryStore("http://controller:8082")
	runner := NewRunner(client, 1)
	runner.SetArtifacts(store)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if got := run.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("phase = %s, want succeeded", got)
	}
	if len(run.Reports()) != 1 {
		t.Fatalf("reports = %d, want 1", len(run.Reports()))
	}
	if run.ReportRef() == "" {
		t.Fatal("report ref was not restored")
	}
}

func TestRunReportsDeduplicateRanksOnRestore(t *testing.T) {
	run := newTestRun(2)
	run.mu.Lock()
	run.reports = []map[string]any{
		{
			"rank": float64(1),
			"report": map[string]any{
				"status": "staged",
			},
		},
		{
			"rank": float64(1),
			"report": map[string]any{
				"status": "success",
			},
		},
		{
			"rank": float64(0),
			"report": map[string]any{
				"status": "success",
			},
		},
		{
			"rank": float64(1),
			"report": map[string]any{
				"status": "staged",
			},
		},
	}
	run.mu.Unlock()

	reports := run.Reports()
	if len(reports) != 2 {
		t.Fatalf("reports = %d, want 2", len(reports))
	}
	statuses := make(map[string]string, len(reports))
	for _, item := range reports {
		statuses[fmt.Sprint(item["rank"])] = item["report"].(map[string]any)["status"].(string)
	}
	if statuses["1"] != "success" {
		t.Fatalf("last report for rank 1 was not retained: %#v", reports)
	}
}

func TestRunnerNeverCommitsWhenOneRankFails(t *testing.T) {
	run := newTestRun(3)
	client := newFakeTaskClient(run)
	client.failExecuteRank = 1
	runner := NewRunner(client, 3)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	_ = run.Wait(ctx)
	if client.commitCount != 0 {
		t.Fatalf("commits = %d, want 0", client.commitCount)
	}
	if got := run.Summary().Phase; got != PhaseFailed {
		t.Fatalf("phase = %s, want failed", got)
	}
}

func TestRunnerSetsCommitIntentBeforeEveryCommit(t *testing.T) {
	run := newTestRun(3)
	client := newFakeTaskClient(run)
	runner := NewRunner(client, 3)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if client.commitWithoutIntentSeen {
		t.Fatal("a commit was sent before CommitIntent")
	}
}

func TestRunnerReadsBackReportWhenCommitResponseOmitsIt(t *testing.T) {
	run := newTestRun(1)
	client := newFakeTaskClient(run)
	client.omitCommitReport = true
	runner := NewRunner(client, 1)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if got := run.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("phase = %s, want succeeded", got)
	}
	if got := len(run.Reports()); got != 1 {
		t.Fatalf("reports = %d, want 1", got)
	}
}

func TestRunDoesNotSucceedBeforeReportIsFinalized(t *testing.T) {
	run := newTestRun(1)
	run.SetPhase(0, PhaseCommitted, "task", "")
	if got := run.Summary().Phase; got != PhaseFinalizing {
		t.Fatalf("phase = %s, want finalizing", got)
	}
	select {
	case <-run.done:
		t.Fatal("run finished before report was finalized")
	default:
	}
	run.SetReportRef("http://artifact/report")
	if got := run.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("phase = %s, want succeeded", got)
	}
	select {
	case <-run.done:
	default:
		t.Fatal("run did not finish after report was finalized")
	}
}

func TestRunRequestCancelBeforeCommit(t *testing.T) {
	run := newTestRun(2)
	if !run.RequestCancel() {
		t.Fatal("cancel should be accepted before commit intent")
	}
	if got := run.Summary().Phase; got != PhaseCancelled {
		t.Fatalf("phase = %s, want cancelled", got)
	}
	if !run.CancelRequested() {
		t.Fatal("cancel flag was not recorded")
	}
	if run.RequestCancel() != true {
		t.Fatal("repeated cancel should be idempotent")
	}
}

func TestRunCancelPreventsCommitIntent(t *testing.T) {
	run := newTestRun(1)
	run.SetPhase(0, PhaseStaged, "task", "")
	if !run.RequestCancel() {
		t.Fatal("cancel should be accepted before commit intent")
	}
	if _, ok := run.TryBeginCommit(); ok {
		t.Fatal("cancelled run must not enter commit intent")
	}
}

func TestRunnerPublishesAggregatedReportArtifact(t *testing.T) {
	run := newTestRun(2)
	client := newFakeTaskClient(run)
	store := artifactstore.NewMemoryStore("http://controller:8082")
	runner := NewRunner(client, 2)
	runner.SetArtifacts(store)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for run.ReportRef() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if run.ReportRef() == "" {
		t.Fatal("report artifact ref is empty")
	}
	if _, err := store.Get(context.Background(), run.ReportRef()); err != nil {
		t.Fatalf("read report artifact: %v", err)
	}
}

type fakeProofMerger struct {
	calls int
}

func (f *fakeProofMerger) MergeProof(
	_ context.Context,
	_ string,
	proofs []map[string]any,
) (map[string]any, error) {
	f.calls++
	if len(proofs) == 0 {
		return nil, fmt.Errorf("no proofs")
	}
	return map[string]any{
		"version": "1",
		"root":    "sha256:" + strings.Repeat("b", 64),
	}, nil
}

func TestRunnerPublishesMergedProofArtifact(t *testing.T) {
	run := newTestRun(2)
	client := newFakeTaskClient(run)
	client.withProof = true
	store := artifactstore.NewMemoryStore("http://controller:8082")
	merger := &fakeProofMerger{}
	runner := NewRunner(client, 2)
	runner.SetArtifacts(store)
	runner.SetProofMerger(merger)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for run.ProofRef() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if run.ProofRef() == "" {
		t.Fatal("proof artifact ref is empty")
	}
	if run.ProofHash() != "sha256:"+strings.Repeat("b", 64) {
		t.Fatalf("proof hash = %q", run.ProofHash())
	}
	if merger.calls != 1 {
		t.Fatalf("proof merger calls = %d, want 1", merger.calls)
	}
	payload, err := store.Get(context.Background(), run.ProofRef())
	if err != nil {
		t.Fatalf("read proof artifact: %v", err)
	}
	var proof map[string]any
	if err := json.Unmarshal(payload, &proof); err != nil {
		t.Fatal(err)
	}
	if proof["root"] != run.ProofHash() {
		t.Fatalf("proof artifact root = %v", proof["root"])
	}
	deadline = time.Now().Add(time.Second)
	for run.ReportRef() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if run.ReportRef() == "" {
		t.Fatal("report artifact ref is empty")
	}
	reportPayload, err := store.Get(context.Background(), run.ReportRef())
	if err != nil {
		t.Fatalf("read report artifact: %v", err)
	}
	var aggregate map[string]any
	if err := json.Unmarshal(reportPayload, &aggregate); err != nil {
		t.Fatal(err)
	}
	reports, _ := aggregate["reports"].([]any)
	if len(reports) != 2 {
		t.Fatalf("aggregated reports = %d, want 2", len(reports))
	}
	for _, raw := range reports {
		row, _ := raw.(map[string]any)
		report, _ := row["report"].(map[string]any)
		if report["proof_ref"] != run.ProofRef() ||
			report["proof_hash"] != run.ProofHash() {
			t.Fatalf("report proof refs = %v", report)
		}
	}
}

func TestRunSnapshotRestoresCommittedState(t *testing.T) {
	run := newTestRun(2)
	client := newFakeTaskClient(run)
	store := artifactstore.NewMemoryStore("http://controller:8082")
	runner := NewRunner(client, 2)
	runner.SetArtifacts(store)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runner.Start(ctx)
	if err := run.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	runner.Stop()
	if err := runner.Persist(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for run.ExecutionRef() == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if run.ExecutionRef() == "" {
		t.Fatal("execution ref is empty")
	}

	restoredRunner := NewRunner(client, 1)
	restoredRunner.SetArtifacts(store)
	restored, err := restoredRunner.Load(context.Background(), run.ExecutionRef())
	if err != nil {
		t.Fatal(err)
	}
	if got := restored.Summary().Phase; got != PhaseSucceeded {
		t.Fatalf("restored phase = %s, want succeeded", got)
	}
	if len(restored.Reports()) != 2 {
		t.Fatalf("restored reports = %d, want 2", len(restored.Reports()))
	}
}

func TestRunnerTaskTimeoutFailsRank(t *testing.T) {
	run := newTestRun(1)
	client := newFakeTaskClient(run)
	client.blockCreate = true
	runner := NewRunner(client, 1)
	runner.SetTaskTimeout(20 * time.Millisecond)
	runner.Submit(run)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	runner.Start(ctx)
	defer runner.Stop()
	err := run.Wait(ctx)
	if err == nil {
		t.Fatal("expected timeout failure")
	}
	if got := run.Summary().Phase; got != PhaseFailed {
		t.Fatalf("phase = %s, want failed", got)
	}
}

func TestSnapshotStoresPlanByReference(t *testing.T) {
	run := newTestRun(1)
	run.config.Plan = map[string]any{
		"version": "1",
		"nodes": []any{
			map[string]any{"target_rank": float64(0), "id": "n0"},
		},
	}
	store := artifactstore.NewMemoryStore("http://controller:8082")
	planBytes, err := json.Marshal(run.config.Plan)
	if err != nil {
		t.Fatal(err)
	}
	planRef, err := store.Put(context.Background(), planBytes)
	if err != nil {
		t.Fatal(err)
	}
	run.config.PlanRef = planRef.URL
	if plan := run.Snapshot().Config.Plan; plan != nil {
		t.Fatal("snapshot embedded the full plan")
	}
	snapshotBytes, err := json.Marshal(run.Snapshot())
	if err != nil {
		t.Fatal(err)
	}
	snapshotRef, err := store.Put(context.Background(), snapshotBytes)
	if err != nil {
		t.Fatal(err)
	}
	runner := NewRunner(newFakeTaskClient(run), 1)
	runner.SetArtifacts(store)
	restored, err := runner.Load(context.Background(), snapshotRef.URL)
	if err != nil {
		t.Fatal(err)
	}
	slice := restored.TaskForRank(0).PlanSlice
	nodes := slice["nodes"].([]any)
	if len(nodes) != 1 {
		t.Fatalf("reloaded plan slice = %v", slice)
	}
}
