package main

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func TestNextActionForTargetFailure(t *testing.T) {
	migration := &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "team-a"},
		Status: aevrelv1.CheckpointMigrationStatus{
			Phase: aevrelv1.MigrationPhaseTargetFailed,
			Conditions: []metav1.Condition{{
				Type:   "TargetReady",
				Reason: "Incomplete",
			}},
		},
	}
	action := nextAction(migration)
	for _, want := range []string{"describe migration team-a/job42", "retry"} {
		if !strings.Contains(action, want) {
			t.Fatalf("next action missing %q: %s", want, action)
		}
	}
}

func TestNextActionForLiveExecutorUnavailable(t *testing.T) {
	migration := &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "team-a"},
		Status: aevrelv1.CheckpointMigrationStatus{
			Phase: aevrelv1.MigrationPhaseFailed,
			Conditions: []metav1.Condition{{
				Type:   "LiveReady",
				Reason: "LiveExecutorUnavailable",
			}},
		},
	}
	action := nextAction(migration)
	for _, want := range []string{"controller.liveURL", "never falls back"} {
		if !strings.Contains(action, want) {
			t.Fatalf("next action missing %q: %s", want, action)
		}
	}
}
