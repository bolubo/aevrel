package execution

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"github.com/shardferry/shardferry/internal/agentclient"
)

type Phase string

const (
	PhasePending          Phase = "pending"
	PhasePrepared         Phase = "prepared"
	PhaseStaged           Phase = "staged"
	PhaseCommitting       Phase = "committing"
	PhaseCommitted        Phase = "committed"
	PhaseFinalizing       Phase = "finalizing"
	PhaseFailed           Phase = "failed"
	PhaseCommitIncomplete Phase = "commit-incomplete"
	PhaseCancelled        Phase = "cancelled"
	PhaseSucceeded        Phase = "succeeded"
)

type RankState struct {
	Rank     int32
	Phase    Phase
	TaskID   string
	Attempts int
	Error    string
}

type RunConfig struct {
	ID             string
	PlanID         string
	MigrationUID   string
	TargetWorld    int32
	SpecRef        string
	PlanRef        string
	RootTemplate   string
	Plan           map[string]any
	Endpoints      map[int32]string
	MaxAttempts    int
	MaxConcurrency int
	CommitToken    string
	RequireReport  bool
}

type Summary struct {
	RunID        string
	Phase        Phase
	Registered   int
	Prepared     int
	Staged       int
	Committed    int
	Failed       int
	CommitIntent bool
}

type RunSnapshot struct {
	Config          RunConfig        `json:"config"`
	Ranks           []RankState      `json:"ranks"`
	CommitIntent    bool             `json:"commit_intent"`
	Reports         []map[string]any `json:"reports,omitempty"`
	ReportRef       string           `json:"report_ref,omitempty"`
	ProofRef        string           `json:"proof_ref,omitempty"`
	ProofHash       string           `json:"proof_hash,omitempty"`
	ExecutionRef    string           `json:"execution_ref,omitempty"`
	TerminalError   string           `json:"terminal_error,omitempty"`
	CancelRequested bool             `json:"cancel_requested,omitempty"`
}

type Run struct {
	config RunConfig

	mu              sync.RWMutex
	ranks           map[int32]*RankState
	reports         []map[string]any
	reportRef       string
	proofRef        string
	proofHash       string
	executionRef    string
	commitIntent    bool
	cancelRequested bool
	done            chan struct{}
	doneOnce        sync.Once
	terminalErr     error
	finalizing      bool
}

func NewRun(config RunConfig) *Run {
	return &Run{
		config: config,
		ranks:  make(map[int32]*RankState),
		done:   make(chan struct{}),
	}
}

func (r *Run) RegisterRank(rank int32) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if rank >= 0 && rank < r.config.TargetWorld {
		if _, exists := r.ranks[rank]; !exists {
			r.ranks[rank] = &RankState{Rank: rank, Phase: PhasePending}
		}
	}
}

func (r *Run) SetPhase(rank int32, phase Phase, taskID, errorText string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	state := r.ranks[rank]
	if state == nil {
		return
	}
	previous := state.Phase
	state.Phase = phase
	if taskID != "" {
		state.TaskID = taskID
	}
	state.Error = errorText
	if phase == PhaseFailed || phase == PhaseCommitIncomplete {
		state.Attempts++
	}
	if r.allCommittedLocked() &&
		(!r.config.RequireReport || r.reportRef != "") {
		r.finishLocked(nil)
	}
	observeRankPhaseTransition(previous, phase)
}

// RestorePhase applies a target task state observed after a controller
// restart. Unlike SetPhase, it does not consume a retry attempt.
func (r *Run) RestorePhase(rank int32, phase Phase, taskID, errorText string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	state := r.ranks[rank]
	if state == nil {
		return
	}
	state.Phase = phase
	if taskID != "" {
		state.TaskID = taskID
	}
	state.Error = errorText
	if phase == PhaseCommitting || phase == PhaseCommitIncomplete {
		r.commitIntent = true
	}
}

func (r *Run) TryBeginCommit() ([]int32, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cancelRequested {
		return nil, false
	}
	if r.commitIntent {
		return r.ranksToCommitLocked(), true
	}
	if len(r.ranks) != int(r.config.TargetWorld) {
		return nil, false
	}
	for _, state := range r.ranks {
		if state.Phase != PhaseStaged && state.Phase != PhaseCommitted {
			return nil, false
		}
	}
	r.commitIntent = true
	ranks := r.ranksToCommitLocked()
	for _, rank := range ranks {
		r.ranks[rank].Phase = PhaseCommitting
	}
	return ranks, true
}

func (r *Run) Summary() Summary {
	r.mu.RLock()
	defer r.mu.RUnlock()
	summary := Summary{
		RunID:        r.config.ID,
		Registered:   len(r.ranks),
		CommitIntent: r.commitIntent,
	}
	for _, state := range r.ranks {
		switch state.Phase {
		case PhasePrepared:
			summary.Prepared++
		case PhaseStaged:
			summary.Staged++
		case PhaseCommitting:
			summary.Staged++
		case PhaseCommitted:
			summary.Committed++
			summary.Staged++
		case PhaseFailed:
			summary.Failed++
		case PhaseCommitIncomplete:
			summary.Failed++
		}
	}
	switch {
	case r.terminalErr != nil:
		summary.Phase = PhaseFailed
	case hasPhase(r.ranks, PhaseCommitIncomplete):
		summary.Phase = PhaseCommitIncomplete
	case summary.Failed > 0:
		summary.Phase = PhaseFailed
	case hasPhase(r.ranks, PhaseCancelled):
		summary.Phase = PhaseCancelled
	case r.allCommittedLocked():
		if r.config.RequireReport && r.reportRef == "" {
			summary.Phase = PhaseFinalizing
		} else {
			summary.Phase = PhaseSucceeded
		}
	case r.commitIntent:
		summary.Phase = PhaseCommitting
	case len(r.ranks) == int(r.config.TargetWorld) && summary.Staged == len(r.ranks):
		summary.Phase = PhaseStaged
	case summary.Prepared > 0:
		summary.Phase = PhasePrepared
	default:
		summary.Phase = PhasePending
	}
	return summary
}

func hasPhase(ranks map[int32]*RankState, phase Phase) bool {
	for _, state := range ranks {
		if state.Phase == phase {
			return true
		}
	}
	return false
}

func (r *Run) Wait(ctx context.Context) error {
	select {
	case <-r.done:
		r.mu.RLock()
		defer r.mu.RUnlock()
		return r.terminalErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Run) FailTerminal(err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.finishLocked(err)
}

func (r *Run) Endpoint(rank int32) string {
	return r.config.Endpoints[rank]
}

func (r *Run) HasCommitIntent() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.commitIntent
}

func (r *Run) Rank(rank int32) RankState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	state := r.ranks[rank]
	if state == nil {
		return RankState{}
	}
	return *state
}

func (r *Run) Ranks() []RankState {
	r.mu.RLock()
	defer r.mu.RUnlock()
	result := make([]RankState, 0, len(r.ranks))
	for _, rank := range r.ranksInOrderLocked() {
		result = append(result, *r.ranks[rank])
	}
	return result
}

func (r *Run) RepairCommitIncomplete() ([]int32, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.commitIntent {
		return nil, fmt.Errorf("run has not reached commit intent")
	}
	if r.cancelRequested {
		return nil, fmt.Errorf("run was cancelled")
	}
	ranks := make([]int32, 0)
	for _, rank := range r.ranksInOrderLocked() {
		state := r.ranks[rank]
		if state.Phase != PhaseCommitIncomplete {
			continue
		}
		state.Phase = PhaseCommitting
		state.Attempts = 0
		state.Error = ""
		ranks = append(ranks, rank)
	}
	if len(ranks) == 0 {
		if r.allCommittedLocked() {
			return nil, nil
		}
		return nil, fmt.Errorf("run has no commit-incomplete ranks")
	}
	r.terminalErr = nil
	r.done = make(chan struct{})
	r.doneOnce = sync.Once{}
	return ranks, nil
}

func (r *Run) AddReport(rank int32, report map[string]any) {
	if report == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	rankText := fmt.Sprint(rank)
	for index, item := range r.reports {
		if fmt.Sprint(item["rank"]) == rankText {
			r.reports[index] = map[string]any{
				"rank":   rank,
				"report": report,
			}
			return
		}
	}
	r.reports = append(r.reports, map[string]any{
		"rank":   rank,
		"report": report,
	})
}

func (r *Run) Reports() []map[string]any {
	r.mu.RLock()
	defer r.mu.RUnlock()
	byRank := make(map[string]map[string]any, len(r.reports))
	order := make([]string, 0, len(r.reports))
	for _, item := range r.reports {
		rank := fmt.Sprint(item["rank"])
		if _, exists := byRank[rank]; !exists {
			order = append(order, rank)
		}
		if previous, exists := byRank[rank]; exists &&
			reportPriority(item) < reportPriority(previous) {
			continue
		}
		byRank[rank] = item
	}
	result := make([]map[string]any, 0, len(order))
	for _, rank := range order {
		result = append(result, byRank[rank])
	}
	return result
}

func reportPriority(item map[string]any) int {
	report, ok := item["report"].(map[string]any)
	if !ok {
		return 0
	}
	if _, ok := report["proof"]; ok {
		return 2
	}
	if report["status"] == "success" {
		return 1
	}
	return 0
}

func (r *Run) SetReportRef(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.reportRef = ref
	if r.allCommittedLocked() &&
		(!r.config.RequireReport || r.reportRef != "") {
		r.finishLocked(nil)
	}
}

func (r *Run) TryBeginFinalize() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.allCommittedLocked() ||
		!r.config.RequireReport ||
		r.reportRef != "" ||
		r.finalizing ||
		r.terminalErr != nil {
		return false
	}
	r.finalizing = true
	return true
}

func (r *Run) FinishFinalize() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.finalizing = false
	if r.allCommittedLocked() &&
		(!r.config.RequireReport || r.reportRef != "") {
		r.finishLocked(nil)
	}
}

func (r *Run) SetReportRequired(required bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.config.RequireReport = required
	if !required && r.allCommittedLocked() {
		r.finishLocked(nil)
	}
}

func (r *Run) PlanRef() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.config.PlanRef
}

func (r *Run) ReportRef() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.reportRef
}

func (r *Run) SetProof(ref, hash string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.proofRef = ref
	r.proofHash = hash
}

func (r *Run) ProofRef() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.proofRef
}

func (r *Run) ProofHash() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.proofHash
}

func (r *Run) RequestCancel() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.commitIntent || r.allCommittedLocked() {
		return false
	}
	if r.cancelRequested {
		return true
	}
	r.cancelRequested = true
	for _, state := range r.ranks {
		if state.Phase != PhaseCommitted && state.Phase != PhaseCommitting {
			state.Phase = PhaseCancelled
		}
	}
	r.finishLocked(nil)
	return true
}

func (r *Run) CancelRequested() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.cancelRequested
}

func (r *Run) SetPlan(plan map[string]any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.config.Plan = plan
}

func (r *Run) ExecutionRef() string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.executionRef
}

func (r *Run) SetExecutionRef(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.executionRef = ref
}

func (r *Run) Snapshot() RunSnapshot {
	r.mu.RLock()
	defer r.mu.RUnlock()
	ranks := make([]RankState, 0, len(r.ranks))
	for _, rank := range r.ranksInOrderLocked() {
		ranks = append(ranks, *r.ranks[rank])
	}
	reports := make([]map[string]any, len(r.reports))
	copy(reports, r.reports)
	terminal := ""
	if r.terminalErr != nil {
		terminal = r.terminalErr.Error()
	}
	config := r.config
	config.Plan = nil
	return RunSnapshot{
		Config:          config,
		Ranks:           ranks,
		CommitIntent:    r.commitIntent,
		Reports:         reports,
		ReportRef:       r.reportRef,
		ProofRef:        r.proofRef,
		ProofHash:       r.proofHash,
		ExecutionRef:    r.executionRef,
		TerminalError:   terminal,
		CancelRequested: r.cancelRequested,
	}
}

func FromSnapshot(snapshot RunSnapshot) *Run {
	run := NewRun(snapshot.Config)
	run.mu.Lock()
	defer run.mu.Unlock()
	for _, state := range snapshot.Ranks {
		copyState := state
		run.ranks[state.Rank] = &copyState
	}
	run.commitIntent = snapshot.CommitIntent
	run.reports = append([]map[string]any(nil), snapshot.Reports...)
	run.reportRef = snapshot.ReportRef
	run.proofRef = snapshot.ProofRef
	run.proofHash = snapshot.ProofHash
	run.executionRef = snapshot.ExecutionRef
	run.cancelRequested = snapshot.CancelRequested
	if snapshot.CancelRequested {
		run.doneOnce.Do(func() { close(run.done) })
	} else if snapshot.TerminalError != "" {
		run.terminalErr = errors.New(snapshot.TerminalError)
		run.doneOnce.Do(func() { close(run.done) })
	} else if run.allCommittedLocked() &&
		(!run.config.RequireReport || run.reportRef != "") {
		run.doneOnce.Do(func() { close(run.done) })
	}
	return run
}

func (r *Run) MaxAttempts() int {
	if r.config.MaxAttempts < 1 {
		return 3
	}
	return r.config.MaxAttempts
}

func (r *Run) MaxConcurrency() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.config.MaxConcurrency
}

func (r *Run) TaskForRank(rank int32) agentclient.Task {
	targetRoot := strings.ReplaceAll(
		r.config.RootTemplate,
		"{rank}",
		fmt.Sprint(rank),
	)
	task := agentclient.Task{
		Version:      "1",
		MigrationUID: r.config.MigrationUID,
		CommitToken:  r.config.CommitToken,
		Rank:         rank,
		TargetRoot:   targetRoot,
		SpecRef:      r.config.SpecRef,
		PlanRef:      r.config.PlanRef,
		PlanSlice:    planSlice(r.config.Plan, rank),
		State:        "created",
	}
	task.TaskHash = taskHash(task)
	return task
}

func (r *Run) finishLocked(err error) {
	r.terminalErr = err
	r.doneOnce.Do(func() { close(r.done) })
}

func (r *Run) allCommittedLocked() bool {
	if len(r.ranks) != int(r.config.TargetWorld) {
		return false
	}
	for _, state := range r.ranks {
		if state.Phase != PhaseCommitted {
			return false
		}
	}
	return true
}

func (r *Run) ranksInOrderLocked() []int32 {
	ranks := make([]int32, 0, len(r.ranks))
	for rank := range r.ranks {
		ranks = append(ranks, rank)
	}
	sort.Slice(ranks, func(i, j int) bool { return ranks[i] < ranks[j] })
	return ranks
}

func (r *Run) ranksToCommitLocked() []int32 {
	ranks := make([]int32, 0, len(r.ranks))
	for rank, state := range r.ranks {
		if state.Phase != PhaseCommitted {
			ranks = append(ranks, rank)
		}
	}
	sort.Slice(ranks, func(i, j int) bool { return ranks[i] < ranks[j] })
	return ranks
}

func planSlice(plan map[string]any, rank int32) map[string]any {
	nodes, _ := plan["nodes"].([]any)
	filtered := make([]any, 0)
	for _, node := range nodes {
		row, ok := node.(map[string]any)
		if ok && int32Value(row["target_rank"]) == rank {
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
