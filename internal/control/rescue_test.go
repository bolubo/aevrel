package control

import (
	"testing"
	"time"
)

func TestDecideRescueChoosesVerifiedRestoreAndMigrate(t *testing.T) {
	loadable := true
	movable := true
	readiness := &CheckpointReadiness{
		Complete:  true,
		Fresh:     true,
		Loadable:  &loadable,
		Movable:   &movable,
		LastDrill: "default/drill",
	}
	now := time.Date(2026, 10, 22, 0, 0, 0, 0, time.UTC)
	restore := DecideRescue("Preempted", "", "hold", readiness, now)
	if restore.Action != RescueRestore ||
		restore.Code != RescueReasonRestoreReady ||
		restore.ProofRef != "default/drill" {
		t.Fatalf("restore decision = %+v", restore)
	}
	migrate := DecideRescue("Preempted", "cluster-b", "hold", readiness, now)
	if migrate.Action != RescueMigrate ||
		migrate.Code != RescueReasonMigrateReady ||
		migrate.Target != "cluster-b" {
		t.Fatalf("migrate decision = %+v", migrate)
	}
}

func TestDecideRescueFailsClosedWithoutProof(t *testing.T) {
	loadable := true
	readiness := &CheckpointReadiness{
		Complete: true,
		Fresh:    true,
		Loadable: &loadable,
	}
	decision := DecideRescue(
		"Preempted",
		"",
		"hold",
		readiness,
		time.Now(),
	)
	if decision.Action != RescueHold ||
		decision.Code != RescueReasonProofMissing {
		t.Fatalf("decision = %+v", decision)
	}

	restart := DecideRescue(
		"Preempted",
		"",
		"restart",
		nil,
		time.Now(),
	)
	if restart.Action != RescueRestart ||
		restart.Code != RescueReasonRestartFallback {
		t.Fatalf("restart decision = %+v", restart)
	}
}

func TestDecideRescueHoldsStaleIncompleteAndBlockedTargets(t *testing.T) {
	loadable := true
	movable := false
	cases := []struct {
		name      string
		readiness *CheckpointReadiness
		target    string
		code      string
	}{
		{
			name: "missing checkpoint",
			code: RescueReasonCheckpointMissing,
		},
		{
			name: "incomplete",
			readiness: &CheckpointReadiness{
				Fresh: true,
			},
			code: RescueReasonCheckpointIncomplete,
		},
		{
			name: "stale",
			readiness: &CheckpointReadiness{
				Complete: true,
			},
			code: RescueReasonCheckpointStale,
		},
		{
			name: "blocked target",
			readiness: &CheckpointReadiness{
				Complete:  true,
				Fresh:     true,
				Loadable:  &loadable,
				Movable:   &movable,
				LastDrill: "default/drill",
			},
			target: "cluster-b",
			code:   RescueReasonTargetBlocked,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			decision := DecideRescue(
				"Preempted",
				testCase.target,
				"hold",
				testCase.readiness,
				time.Now(),
			)
			if decision.Action != RescueHold || decision.Code != testCase.code {
				t.Fatalf("decision = %+v", decision)
			}
		})
	}
}
