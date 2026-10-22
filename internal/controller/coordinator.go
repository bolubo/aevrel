package controller

import (
	"errors"
	"fmt"
	"sort"
)

type TaskPhase string

const (
	TaskCreated          TaskPhase = "created"
	TaskPreparing        TaskPhase = "preparing"
	TaskPrepared         TaskPhase = "prepared"
	TaskTransferring     TaskPhase = "transferring"
	TaskStaged           TaskPhase = "staged"
	TaskCommitReady      TaskPhase = "commit-ready"
	TaskCommitting       TaskPhase = "committing"
	TaskCommitted        TaskPhase = "committed"
	TaskFailed           TaskPhase = "failed"
	TaskCancelled        TaskPhase = "cancelled"
	TaskCommitIncomplete TaskPhase = "commit-incomplete"
)

type MigrationPhase string

const (
	MigrationPending          MigrationPhase = "pending"
	MigrationPreparing        MigrationPhase = "preparing"
	MigrationTransferring     MigrationPhase = "transferring"
	MigrationCommitPending    MigrationPhase = "commit-pending"
	MigrationCommitting       MigrationPhase = "committing"
	MigrationSucceeded        MigrationPhase = "succeeded"
	MigrationFailed           MigrationPhase = "failed"
	MigrationCancelled        MigrationPhase = "cancelled"
	MigrationCommitIncomplete MigrationPhase = "commit-incomplete"
)

type RankCounts struct {
	Prepared  int `json:"prepared"`
	Staged    int `json:"staged"`
	Committed int `json:"committed"`
	Failed    int `json:"failed"`
}

type Summary struct {
	MigrationUID    string         `json:"migration_uid"`
	Phase           MigrationPhase `json:"phase"`
	CanCommit       bool           `json:"can_commit"`
	CommitStarted   bool           `json:"commit_started"`
	RegisteredRanks int            `json:"registered_ranks"`
	TargetWorldSize int            `json:"target_world_size"`
	FailedRanks     []int          `json:"failed_ranks"`
	Counts          RankCounts     `json:"counts"`
}

type Coordinator struct {
	migrationUID  string
	targetWorld   int
	ranks         map[int]TaskPhase
	errors        map[int]string
	commitStarted bool
}

func NewCoordinator(migrationUID string, targetWorldSize int) *Coordinator {
	return &Coordinator{
		migrationUID: migrationUID,
		targetWorld:  targetWorldSize,
		ranks:        make(map[int]TaskPhase),
		errors:       make(map[int]string),
	}
}

func (c *Coordinator) RegisterRank(rank int) error {
	if err := c.checkRank(rank); err != nil {
		return err
	}
	if _, exists := c.ranks[rank]; !exists {
		c.ranks[rank] = TaskCreated
	}
	return nil
}

func (c *Coordinator) SetRankState(
	rank int,
	state TaskPhase,
	message string,
) error {
	if err := c.checkRank(rank); err != nil {
		return err
	}
	if _, exists := c.ranks[rank]; !exists {
		return fmt.Errorf("rank %d is not registered", rank)
	}
	c.ranks[rank] = state
	if state == TaskFailed || state == TaskCommitIncomplete {
		if message == "" {
			message = string(state)
		}
		c.errors[rank] = message
	} else {
		delete(c.errors, rank)
	}
	return nil
}

func (c *Coordinator) CanCommit() bool {
	if c.commitStarted || len(c.ranks) != c.targetWorld {
		return false
	}
	for _, state := range c.ranks {
		if state != TaskStaged {
			return false
		}
	}
	return true
}

func (c *Coordinator) BeginCommit() ([]int, error) {
	if !c.CanCommit() {
		return nil, errors.New("commit barrier is not ready")
	}
	ranks := make([]int, 0, len(c.ranks))
	for rank := range c.ranks {
		c.ranks[rank] = TaskCommitting
		ranks = append(ranks, rank)
	}
	sort.Ints(ranks)
	c.commitStarted = true
	return ranks, nil
}

func (c *Coordinator) Summary() Summary {
	failedRanks := make([]int, 0)
	for rank, state := range c.ranks {
		if state == TaskFailed || state == TaskCommitIncomplete {
			failedRanks = append(failedRanks, rank)
		}
	}
	sort.Ints(failedRanks)
	phase := c.phase()
	counts := c.counts()
	return Summary{
		MigrationUID:    c.migrationUID,
		Phase:           phase,
		CanCommit:       c.CanCommit(),
		CommitStarted:   c.commitStarted,
		RegisteredRanks: len(c.ranks),
		TargetWorldSize: c.targetWorld,
		FailedRanks:     failedRanks,
		Counts:          counts,
	}
}

func (c *Coordinator) phase() MigrationPhase {
	states := make([]TaskPhase, 0, len(c.ranks))
	for _, state := range c.ranks {
		states = append(states, state)
	}
	if containsState(states, TaskCommitIncomplete) {
		return MigrationCommitIncomplete
	}
	if c.hasFailed() {
		if c.commitStarted {
			return MigrationCommitIncomplete
		}
		return MigrationFailed
	}
	if c.commitStarted {
		if len(states) > 0 && allState(states, TaskCommitted) {
			return MigrationSucceeded
		}
		return MigrationCommitting
	}
	if len(c.ranks) == c.targetWorld && allState(states, TaskStaged) {
		return MigrationCommitPending
	}
	if containsState(states, TaskTransferring) || containsState(states, TaskStaged) {
		return MigrationTransferring
	}
	if containsState(states, TaskPreparing) || containsState(states, TaskPrepared) {
		return MigrationPreparing
	}
	if containsState(states, TaskCancelled) {
		return MigrationCancelled
	}
	return MigrationPending
}

func (c *Coordinator) counts() RankCounts {
	var counts RankCounts
	for _, state := range c.ranks {
		switch state {
		case TaskPrepared, TaskTransferring, TaskStaged, TaskCommitReady, TaskCommitting, TaskCommitted:
			counts.Prepared++
		}
		switch state {
		case TaskStaged, TaskCommitReady, TaskCommitting, TaskCommitted:
			counts.Staged++
		}
		switch state {
		case TaskCommitted:
			counts.Committed++
		case TaskFailed, TaskCommitIncomplete:
			counts.Failed++
		}
	}
	return counts
}

func (c *Coordinator) hasFailed() bool {
	for _, state := range c.ranks {
		if state == TaskFailed || state == TaskCommitIncomplete {
			return true
		}
	}
	return false
}

func (c *Coordinator) checkRank(rank int) error {
	if rank < 0 || rank >= c.targetWorld {
		return fmt.Errorf("rank %d is outside target world %d", rank, c.targetWorld)
	}
	return nil
}

func containsState(states []TaskPhase, target TaskPhase) bool {
	for _, state := range states {
		if state == target {
			return true
		}
	}
	return false
}

func allState(states []TaskPhase, target TaskPhase) bool {
	if len(states) == 0 {
		return false
	}
	for _, state := range states {
		if state != target {
			return false
		}
	}
	return true
}
