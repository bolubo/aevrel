package main

import (
	"strings"
	"testing"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func TestStaticProgressLine(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		Spec: shardferryv1.CheckpointMigrationSpec{
			Target: shardferryv1.TargetSpec{WorldSize: 4},
		},
		Status: shardferryv1.CheckpointMigrationStatus{
			Phase: shardferryv1.MigrationPhaseTransferring,
			Counts: shardferryv1.RankCounts{
				Prepared:  4,
				Staged:    3,
				Committed: 2,
			},
		},
	}
	line := migrationProgressLine(migration, false)
	for _, want := range []string{
		"static [",
		"committed=2/4",
		"staged=3",
		"phase=transferring",
	} {
		if !strings.Contains(line, want) {
			t.Fatalf("progress missing %q: %s", want, line)
		}
	}
}

func TestLiveProgressLine(t *testing.T) {
	migration := &shardferryv1.CheckpointMigration{
		Spec: shardferryv1.CheckpointMigrationSpec{
			Target: shardferryv1.TargetSpec{WorldSize: 4},
			Live: &shardferryv1.LiveRankSplitSpec{
				MaxInFlight: 2,
			},
		},
		Status: shardferryv1.CheckpointMigrationStatus{
			Live: &shardferryv1.LiveRankSplitStatus{
				CutoverPhase:  "following",
				InFlight:      []int32{2},
				Coverage:      []int32{0, 1, 3},
				Quorum:        3,
				StateComplete: false,
			},
		},
	}
	line := migrationProgressLine(migration, false)
	for _, want := range []string{
		"live [",
		"settled=2/4",
		"coverage=3/4",
		"inFlight=1/2",
		"quorum=3/3",
		"stateComplete=false",
		"phase=following",
	} {
		if !strings.Contains(line, want) {
			t.Fatalf("progress missing %q: %s", want, line)
		}
	}
}
