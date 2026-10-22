package execution

import (
	"testing"
)

func TestRunNeedsEveryRankBeforeCommitIntent(t *testing.T) {
	run := NewRun(RunConfig{ID: "m", TargetWorld: 3})
	for rank := int32(0); rank < 3; rank++ {
		run.RegisterRank(rank)
	}
	run.SetPhase(0, PhaseStaged, "t0", "")
	run.SetPhase(1, PhaseStaged, "t1", "")
	if _, ok := run.TryBeginCommit(); ok {
		t.Fatal("commit must wait for the missing rank")
	}
	run.SetPhase(2, PhaseStaged, "t2", "")
	ranks, ok := run.TryBeginCommit()
	if !ok || len(ranks) != 3 || !run.HasCommitIntent() {
		t.Fatalf("bad barrier: ranks=%v ok=%v intent=%v", ranks, ok, run.HasCommitIntent())
	}
}

func TestRunFailedRankBlocksCommit(t *testing.T) {
	run := NewRun(RunConfig{ID: "m", TargetWorld: 2})
	run.RegisterRank(0)
	run.RegisterRank(1)
	run.SetPhase(0, PhaseStaged, "t0", "")
	run.SetPhase(1, PhaseFailed, "t1", "disk full")
	if _, ok := run.TryBeginCommit(); ok {
		t.Fatal("failed rank must block commit")
	}
	summary := run.Summary()
	if summary.Phase != PhaseFailed || summary.Failed != 1 {
		t.Fatalf("unexpected summary: %+v", summary)
	}
}

func TestRunCommitIntentIsIdempotent(t *testing.T) {
	run := NewRun(RunConfig{ID: "m", TargetWorld: 1})
	run.RegisterRank(0)
	run.SetPhase(0, PhaseStaged, "t0", "")
	if _, ok := run.TryBeginCommit(); !ok {
		t.Fatal("first barrier should open")
	}
	ranks, ok := run.TryBeginCommit()
	if !ok || len(ranks) != 1 || ranks[0] != 0 {
		t.Fatalf("second barrier: ranks=%v ok=%v", ranks, ok)
	}
}

func TestRunCommitIncompleteIsNotSuccess(t *testing.T) {
	run := NewRun(RunConfig{ID: "m", TargetWorld: 2})
	for rank := int32(0); rank < 2; rank++ {
		run.RegisterRank(rank)
		run.SetPhase(rank, PhaseStaged, "task", "")
	}
	if _, ok := run.TryBeginCommit(); !ok {
		t.Fatal("barrier did not open")
	}
	run.SetPhase(0, PhaseCommitted, "task", "")
	run.SetPhase(1, PhaseCommitIncomplete, "task", "rename failed")
	if got := run.Summary().Phase; got != PhaseCommitIncomplete {
		t.Fatalf("phase = %s, want commit-incomplete", got)
	}
}
