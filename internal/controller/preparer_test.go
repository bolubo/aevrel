package controller

import "testing"

func TestBuildExecutionRunRegistersEveryTargetRank(t *testing.T) {
	migration := validMigration("run-ranks")
	migration.Spec.Target.WorldSize = 4
	migration.Spec.Target.Mesh.Shape = []int32{4}
	migration.Spec.Target.RankMap = migration.Spec.Target.RankMap[:2]
	migration.Spec.Target.RankMap[0].Rank = 0
	migration.Spec.Target.RankMap[1].Rank = 1

	run := buildExecutionRun(migration, Prepared{
		PlanHash: "sha256:" + "a" + string(make([]byte, 63)),
		Plan:     map[string]any{"nodes": []any{}},
	})
	if got, want := len(run.Ranks()), 4; got != want {
		t.Fatalf("registered ranks = %d, want %d", got, want)
	}
	for rank := int32(0); rank < 4; rank++ {
		if got := run.Rank(rank).Phase; got == "" {
			t.Fatalf("rank %d was not registered", rank)
		}
	}
}
