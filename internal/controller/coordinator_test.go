package controller

import (
	"reflect"
	"testing"
)

func TestCommitBarrierWaitsForEveryRank(t *testing.T) {
	coordinator := NewCoordinator("m-1", 3)
	for rank := 0; rank < 3; rank++ {
		coordinator.RegisterRank(rank)
		coordinator.SetRankState(rank, TaskPrepared, "")
	}
	for rank := 0; rank < 2; rank++ {
		coordinator.SetRankState(rank, TaskStaged, "")
	}
	if coordinator.CanCommit() {
		t.Fatal("commit must wait for every rank")
	}
	coordinator.SetRankState(2, TaskStaged, "")
	if !coordinator.CanCommit() {
		t.Fatal("commit should be ready")
	}
	got, err := coordinator.BeginCommit()
	if err != nil {
		t.Fatalf("begin commit: %v", err)
	}
	if want := []int{0, 1, 2}; !reflect.DeepEqual(got, want) {
		t.Fatalf("commit ranks = %v, want %v", got, want)
	}
	if got := coordinator.Summary().Phase; got != MigrationCommitting {
		t.Fatalf("phase = %s, want %s", got, MigrationCommitting)
	}
}

func TestFailedRankBlocksCommit(t *testing.T) {
	coordinator := NewCoordinator("m-2", 2)
	coordinator.RegisterRank(0)
	coordinator.RegisterRank(1)
	coordinator.SetRankState(0, TaskStaged, "")
	coordinator.SetRankState(1, TaskFailed, "disk full")
	summary := coordinator.Summary()
	if coordinator.CanCommit() {
		t.Fatal("failed rank must block commit")
	}
	if summary.Phase != MigrationFailed {
		t.Fatalf("phase = %s, want %s", summary.Phase, MigrationFailed)
	}
	if got, want := summary.FailedRanks, []int{1}; !reflect.DeepEqual(got, want) {
		t.Fatalf("failed ranks = %v, want %v", got, want)
	}
}

func Test128RanksCommitOnlyAfterAllStaged(t *testing.T) {
	coordinator := NewCoordinator("m-128", 128)
	for rank := 0; rank < 128; rank++ {
		coordinator.RegisterRank(rank)
		coordinator.SetRankState(rank, TaskPrepared, "")
	}
	for rank := 0; rank < 127; rank++ {
		coordinator.SetRankState(rank, TaskStaged, "")
	}
	if coordinator.CanCommit() {
		t.Fatal("commit must wait for rank 127")
	}
	coordinator.SetRankState(127, TaskStaged, "")
	ranks, err := coordinator.BeginCommit()
	if err != nil {
		t.Fatalf("begin commit: %v", err)
	}
	if len(ranks) != 128 {
		t.Fatalf("commit ranks = %d, want 128", len(ranks))
	}
	for rank := 0; rank < 128; rank++ {
		coordinator.SetRankState(rank, TaskCommitted, "")
	}
	if got := coordinator.Summary().Phase; got != MigrationSucceeded {
		t.Fatalf("phase = %s, want %s", got, MigrationSucceeded)
	}
}

func TestMissingRankBlocksCommit(t *testing.T) {
	coordinator := NewCoordinator("m-missing", 3)
	coordinator.RegisterRank(0)
	coordinator.RegisterRank(1)
	coordinator.SetRankState(0, TaskStaged, "")
	coordinator.SetRankState(1, TaskStaged, "")
	if coordinator.CanCommit() {
		t.Fatal("missing rank must block commit")
	}
	if _, err := coordinator.BeginCommit(); err == nil {
		t.Fatal("begin commit must fail for missing rank")
	}
}

func TestCommitIncompleteIsNotSuccess(t *testing.T) {
	coordinator := NewCoordinator("m-incomplete", 2)
	for rank := 0; rank < 2; rank++ {
		coordinator.RegisterRank(rank)
		coordinator.SetRankState(rank, TaskStaged, "")
	}
	if _, err := coordinator.BeginCommit(); err != nil {
		t.Fatalf("begin commit: %v", err)
	}
	coordinator.SetRankState(0, TaskCommitted, "")
	coordinator.SetRankState(1, TaskCommitIncomplete, "rename failed")
	summary := coordinator.Summary()
	if summary.Phase != MigrationCommitIncomplete {
		t.Fatalf("phase = %s, want %s", summary.Phase, MigrationCommitIncomplete)
	}
	if summary.CanCommit {
		t.Fatal("commit-incomplete must not be commit-ready")
	}
}
