package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"k8s.io/client-go/util/workqueue"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/artifactstore"
)

type TaskClient interface {
	CreateTask(context.Context, string, agentclient.Task) (agentclient.Task, error)
	PrepareTask(context.Context, string, string) (agentclient.Task, error)
	ExecuteTask(context.Context, string, string) (agentclient.Task, error)
	CommitTask(context.Context, string, string) (agentclient.Task, error)
}

type TaskReader interface {
	GetTask(context.Context, string, string) (agentclient.Task, error)
}

type TaskCanceller interface {
	CancelTask(context.Context, string, string) (agentclient.Task, error)
}

var ErrCannotCancel = errors.New("migration has passed commit intent")

type ProofMerger interface {
	MergeProof(context.Context, string, []map[string]any) (map[string]any, error)
}

type Key struct {
	RunID string
	Rank  int32
}

type Runner struct {
	client      TaskClient
	queue       workqueue.TypedRateLimitingInterface[Key]
	workers     int
	artifacts   artifactstore.Store
	proofs      ProofMerger
	taskTimeout time.Duration

	mu      sync.Mutex
	runs    map[string]*Run
	started bool
	stop    chan struct{}
	wg      sync.WaitGroup
}

func (r *Runner) SetArtifacts(store artifactstore.Store) {
	r.artifacts = store
}

func (r *Runner) SetProofMerger(merger ProofMerger) {
	r.proofs = merger
}

func (r *Runner) SetTaskTimeout(timeout time.Duration) {
	if timeout > 0 {
		r.taskTimeout = timeout
	}
}

func NewRunner(client TaskClient, workers int) *Runner {
	if workers < 1 {
		workers = 4
	}
	return &Runner{
		client: client,
		queue: workqueue.NewTypedRateLimitingQueue(
			workqueue.DefaultTypedControllerRateLimiter[Key](),
		),
		workers:     workers,
		runs:        make(map[string]*Run),
		stop:        make(chan struct{}),
		taskTimeout: 5 * time.Minute,
	}
}

func (r *Runner) Submit(run *Run) {
	_ = r.Ensure(run)
}

func (r *Runner) Ensure(run *Run) *Run {
	r.mu.Lock()
	if _, exists := r.runs[run.config.ID]; exists {
		existing := r.runs[run.config.ID]
		r.mu.Unlock()
		return existing
	}
	r.runs[run.config.ID] = run
	run.mu.RLock()
	ranks := run.ranksInOrderLocked()
	run.mu.RUnlock()
	r.mu.Unlock()
	for _, rank := range ranks {
		r.queue.Add(Key{RunID: run.config.ID, Rank: rank})
	}
	return run
}

func (r *Runner) Get(runID string) *Run {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.runs[runID]
}

func (r *Runner) Cancel(ctx context.Context, run *Run) error {
	states := run.Ranks()
	if !run.RequestCancel() {
		return ErrCannotCancel
	}
	var firstErr error
	if canceller, ok := r.client.(TaskCanceller); ok {
		for _, state := range states {
			if state.TaskID == "" ||
				state.Phase == PhaseCommitted ||
				state.Phase == PhaseCommitting {
				continue
			}
			callCtx, cancel := context.WithTimeout(ctx, r.taskTimeout)
			_, err := canceller.CancelTask(
				callCtx,
				run.Endpoint(state.Rank),
				state.TaskID,
			)
			cancel()
			if err != nil && firstErr == nil {
				firstErr = err
			}
		}
	}
	if err := r.persist(ctx, run); err != nil && firstErr == nil {
		firstErr = err
	}
	return firstErr
}

func (r *Runner) Requeue(run *Run, ranks []int32) {
	for _, rank := range ranks {
		r.queue.Add(Key{RunID: run.config.ID, Rank: rank})
	}
}

func (r *Runner) Start(ctx context.Context) {
	_ = ctx
	r.mu.Lock()
	if r.started {
		r.mu.Unlock()
		return
	}
	r.started = true
	for worker := 0; worker < r.workers; worker++ {
		r.wg.Add(1)
		go r.worker(ctx)
	}
	r.mu.Unlock()
}

func (r *Runner) Stop() {
	r.mu.Lock()
	if !r.started {
		r.mu.Unlock()
		return
	}
	select {
	case <-r.stop:
		r.mu.Unlock()
		return
	default:
		close(r.stop)
	}
	r.mu.Unlock()
	r.queue.ShutDown()
	r.wg.Wait()
}

func (r *Runner) worker(ctx context.Context) {
	defer r.wg.Done()
	for {
		key, shutdown := r.queue.Get()
		if shutdown {
			return
		}
		err := r.process(ctx, key)
		r.queue.Done(key)
		if err != nil {
			r.queue.AddRateLimited(key)
		} else {
			r.queue.Forget(key)
		}
	}
}

func (r *Runner) process(ctx context.Context, key Key) (err error) {
	r.mu.Lock()
	run := r.runs[key.RunID]
	r.mu.Unlock()
	if run == nil {
		return nil
	}
	if run.CancelRequested() {
		return nil
	}
	defer func(processErr *error) {
		if persistErr := r.persist(ctx, run); persistErr != nil && *processErr == nil {
			*processErr = persistErr
		}
	}(&err)
	state := run.Rank(key.Rank)
	switch state.Phase {
	case PhasePending:
		task := run.TaskForRank(key.Rank)
		callCtx, cancel := context.WithTimeout(ctx, r.taskTimeout)
		created, err := r.client.CreateTask(
			callCtx,
			run.Endpoint(key.Rank),
			task,
		)
		cancel()
		if err != nil {
			return r.fail(run, key, err)
		}
		callCtx, cancel = context.WithTimeout(ctx, r.taskTimeout)
		prepared, err := r.client.PrepareTask(
			callCtx,
			run.Endpoint(key.Rank),
			created.TaskID,
		)
		cancel()
		if err != nil {
			return r.fail(run, key, err)
		}
		if prepared.State != "prepared" {
			return r.fail(
				run,
				key,
				fmt.Errorf("rank %d did not prepare: %s", key.Rank, prepared.State),
			)
		}
		if run.CancelRequested() {
			return nil
		}
		run.SetPhase(key.Rank, PhasePrepared, prepared.TaskID, "")
		r.queue.Add(key)
		return nil

	case PhasePrepared:
		callCtx, cancel := context.WithTimeout(ctx, r.taskTimeout)
		task, err := r.client.ExecuteTask(
			callCtx,
			run.Endpoint(key.Rank),
			state.TaskID,
		)
		cancel()
		if err != nil {
			if phase, recovered := r.recoverTask(
				ctx,
				run,
				key,
				state.TaskID,
			); recovered {
				r.resumeRecovered(ctx, run, key, phase)
				return nil
			}
			return r.fail(run, key, err)
		}
		if phase, recovered := r.taskPhase(task); recovered {
			run.RestorePhase(
				key.Rank,
				phase,
				task.TaskID,
				taskError(task, phase),
			)
			r.resumeRecovered(ctx, run, key, phase)
			return nil
		}
		if task.State != "staged" {
			return r.fail(
				run,
				key,
				fmt.Errorf("rank %d did not stage: %s", key.Rank, task.State),
			)
		}
		if run.CancelRequested() {
			return nil
		}
		run.SetPhase(key.Rank, PhaseStaged, task.TaskID, "")
		if ranks, ok := run.TryBeginCommit(); ok {
			for _, rank := range ranks {
				r.queue.Add(Key{RunID: key.RunID, Rank: rank})
			}
		}
		return nil

	case PhaseStaged:
		if phase, recovered := r.recoverTask(
			ctx,
			run,
			key,
			state.TaskID,
		); recovered {
			r.resumeRecovered(ctx, run, key, phase)
			return nil
		}
		if ranks, ok := run.TryBeginCommit(); ok {
			for _, rank := range ranks {
				r.queue.Add(Key{RunID: key.RunID, Rank: rank})
			}
		}
		return nil

	case PhaseCommitting:
		callCtx, cancel := context.WithTimeout(ctx, r.taskTimeout)
		task, err := r.client.CommitTask(
			callCtx,
			run.Endpoint(key.Rank),
			state.TaskID,
		)
		cancel()
		if err != nil {
			run.SetPhase(key.Rank, PhaseCommitIncomplete, state.TaskID, err.Error())
			if !isRetryable(err) {
				run.FailTerminal(err)
				return nil
			}
			if run.Rank(key.Rank).Attempts >= run.MaxAttempts() {
				run.FailTerminal(err)
				return nil
			}
			return err
		}
		if task.State != "committed" {
			err := fmt.Errorf("rank %d did not commit: %s", key.Rank, task.State)
			run.SetPhase(key.Rank, PhaseCommitIncomplete, task.TaskID, err.Error())
			if run.Rank(key.Rank).Attempts >= run.MaxAttempts() {
				run.FailTerminal(err)
				return nil
			}
			return err
		}
		if run.CancelRequested() {
			return nil
		}
		if task.Report == nil {
			if reader, ok := r.client.(TaskReader); ok {
				getCtx, getCancel := context.WithTimeout(ctx, r.taskTimeout)
				if fetched, getErr := reader.GetTask(
					getCtx,
					run.Endpoint(key.Rank),
					task.TaskID,
				); getErr == nil {
					task = fetched
				}
				getCancel()
			}
		}
		if task.Report == nil {
			err := fmt.Errorf("rank %d committed without a report", key.Rank)
			run.SetPhase(
				key.Rank,
				PhaseCommitIncomplete,
				task.TaskID,
				err.Error(),
			)
			if run.Rank(key.Rank).Attempts >= run.MaxAttempts() {
				run.FailTerminal(err)
				return nil
			}
			return err
		}
		run.AddReport(key.Rank, task.Report)
		run.SetPhase(key.Rank, PhaseCommitted, task.TaskID, "")
		return r.maybeFinalize(ctx, run)

	case PhaseFailed:
		if state.Attempts >= run.MaxAttempts() {
			run.FailTerminal(errors.New(state.Error))
			return nil
		}
		run.SetPhase(key.Rank, PhasePending, state.TaskID, "")
		return errors.New(state.Error)

	case PhaseCommitIncomplete:
		if state.Attempts >= run.MaxAttempts() {
			run.FailTerminal(errors.New(state.Error))
			return nil
		}
		run.SetPhase(key.Rank, PhaseCommitting, state.TaskID, "")
		return errors.New(state.Error)

	case PhaseCommitted:
		r.recoverTask(
			ctx,
			run,
			key,
			state.TaskID,
		)
		return r.maybeFinalize(ctx, run)

	default:
		return nil
	}
}

func (r *Runner) recoverTask(
	ctx context.Context,
	run *Run,
	key Key,
	taskID string,
) (Phase, bool) {
	reader, ok := r.client.(TaskReader)
	if !ok || taskID == "" {
		return "", false
	}
	callCtx, cancel := context.WithTimeout(ctx, r.taskTimeout)
	task, err := reader.GetTask(callCtx, run.Endpoint(key.Rank), taskID)
	cancel()
	if err != nil {
		return "", false
	}
	phase, ok := r.taskPhase(task)
	if !ok {
		return "", false
	}
	run.RestorePhase(key.Rank, phase, taskID, taskError(task, phase))
	if task.Report != nil {
		run.AddReport(key.Rank, task.Report)
	}
	return phase, true
}

func (r *Runner) taskPhase(task agentclient.Task) (Phase, bool) {
	switch task.State {
	case "created":
		return PhasePending, true
	case "preparing", "prepared", "transferring":
		return PhasePrepared, true
	case "staged", "commit-ready":
		return PhaseStaged, true
	case "committing":
		return PhaseCommitting, true
	case "commit-incomplete":
		return PhaseCommitIncomplete, true
	case "committed":
		return PhaseCommitted, true
	case "failed":
		return PhaseFailed, true
	case "cancelled":
		return PhaseCancelled, true
	default:
		return "", false
	}
}

func taskError(task agentclient.Task, phase Phase) string {
	if phase == PhaseFailed || phase == PhaseCommitIncomplete {
		return "target task state: " + task.State
	}
	return ""
}

func (r *Runner) resumeRecovered(
	ctx context.Context,
	run *Run,
	key Key,
	phase Phase,
) {
	switch phase {
	case PhasePending, PhasePrepared:
		r.queue.Add(key)
	case PhaseStaged:
		if ranks, ok := run.TryBeginCommit(); ok {
			for _, rank := range ranks {
				r.queue.Add(Key{RunID: key.RunID, Rank: rank})
			}
		}
	case PhaseCommitting, PhaseCommitIncomplete:
		r.queue.Add(key)
	case PhaseCommitted:
		_ = r.maybeFinalize(ctx, run)
	}
}

func (r *Runner) finalizeReport(ctx context.Context, run *Run) error {
	if run == nil || run.ReportRef() != "" {
		return nil
	}
	if r.artifacts == nil {
		run.SetReportRequired(false)
		return nil
	}
	if err := r.publishProof(ctx, run); err != nil {
		return err
	}
	reports := attachProofRefs(run.Reports(), run.ProofRef(), run.ProofHash())
	payload, err := json.Marshal(map[string]any{
		"version":       "1",
		"migration_uid": run.config.MigrationUID,
		"reports":       reports,
	})
	if err != nil {
		return fmt.Errorf("encode report artifact: %w", err)
	}
	ref, err := r.artifacts.Put(ctx, payload)
	if err != nil {
		return fmt.Errorf("publish report artifact: %w", err)
	}
	run.SetReportRef(ref.URL)
	return nil
}

func (r *Runner) maybeFinalize(ctx context.Context, run *Run) error {
	if !run.TryBeginFinalize() {
		return nil
	}
	err := r.finalizeReport(ctx, run)
	run.FinishFinalize()
	return err
}

func (r *Runner) publishProof(ctx context.Context, run *Run) error {
	if r.proofs == nil || run == nil || run.ProofRef() != "" {
		return nil
	}
	proofs := rankProofs(run.Reports())
	if len(proofs) == 0 {
		return nil
	}
	if len(proofs) != run.Summary().Committed {
		return fmt.Errorf(
			"proof reports are incomplete: %d of %d",
			len(proofs),
			run.Summary().Committed,
		)
	}
	endpoints := make([]string, 0)
	seen := make(map[string]struct{})
	for _, state := range run.Ranks() {
		endpoint := run.Endpoint(state.Rank)
		if endpoint == "" {
			continue
		}
		if _, exists := seen[endpoint]; exists {
			continue
		}
		seen[endpoint] = struct{}{}
		endpoints = append(endpoints, endpoint)
	}
	if len(endpoints) == 0 {
		return fmt.Errorf("no target endpoint for proof merge")
	}
	var proof map[string]any
	var lastErr error
	for _, endpoint := range endpoints {
		proof, lastErr = r.proofs.MergeProof(ctx, endpoint, proofs)
		if lastErr == nil {
			break
		}
	}
	if lastErr != nil {
		return fmt.Errorf("merge migration proof: %w", lastErr)
	}
	root, _ := proof["root"].(string)
	if root == "" {
		return fmt.Errorf("merged proof has no root")
	}
	payload, err := json.Marshal(proof)
	if err != nil {
		return fmt.Errorf("encode migration proof: %w", err)
	}
	ref, err := r.artifacts.Put(ctx, payload)
	if err != nil {
		return fmt.Errorf("publish migration proof: %w", err)
	}
	run.SetProof(ref.URL, root)
	return nil
}

func rankProofs(reports []map[string]any) []map[string]any {
	result := make([]map[string]any, 0)
	for _, item := range reports {
		report, ok := item["report"].(map[string]any)
		if !ok {
			continue
		}
		if proof, ok := report["proof"].(map[string]any); ok {
			result = append(result, proof)
		}
	}
	return result
}

func attachProofRefs(
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

func (r *Runner) Persist(ctx context.Context, run *Run) error {
	return r.persist(ctx, run)
}

func (r *Runner) persist(ctx context.Context, run *Run) error {
	if r.artifacts == nil {
		return nil
	}
	payload, err := json.Marshal(run.Snapshot())
	if err != nil {
		return fmt.Errorf("encode execution snapshot: %w", err)
	}
	ref, err := r.artifacts.Put(ctx, payload)
	if err != nil {
		return fmt.Errorf("publish execution snapshot: %w", err)
	}
	run.SetExecutionRef(ref.URL)
	return nil
}

func (r *Runner) Load(ctx context.Context, ref string) (*Run, error) {
	if r.artifacts == nil {
		return nil, fmt.Errorf("artifact store is not configured")
	}
	payload, err := r.artifacts.Get(ctx, ref)
	if err != nil {
		return nil, fmt.Errorf("read execution snapshot: %w", err)
	}
	var snapshot RunSnapshot
	if err := json.Unmarshal(payload, &snapshot); err != nil {
		return nil, fmt.Errorf("decode execution snapshot: %w", err)
	}
	run := FromSnapshot(snapshot)
	if run.config.Plan == nil &&
		run.config.PlanRef != "" &&
		run.Summary().Phase != PhaseSucceeded {
		planBytes, err := r.artifacts.Get(ctx, run.config.PlanRef)
		if err != nil {
			return nil, fmt.Errorf("read plan artifact: %w", err)
		}
		var plan map[string]any
		if err := json.Unmarshal(planBytes, &plan); err != nil {
			return nil, fmt.Errorf("decode plan artifact: %w", err)
		}
		run.SetPlan(plan)
	}
	if run.ExecutionRef() == "" {
		run.SetExecutionRef(ref)
	}
	return run, nil
}

func (r *Runner) fail(run *Run, key Key, err error) error {
	state := run.Rank(key.Rank)
	run.SetPhase(key.Rank, PhaseFailed, state.TaskID, err.Error())
	if !isRetryable(err) {
		run.FailTerminal(err)
		return nil
	}
	if run.Rank(key.Rank).Attempts >= run.MaxAttempts() {
		run.FailTerminal(err)
		return nil
	}
	return err
}
