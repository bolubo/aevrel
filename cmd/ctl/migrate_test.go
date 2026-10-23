package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

const minimalMigration = `
apiVersion: aevrel.dev/v1
kind: CheckpointMigration
metadata:
  name: job42
spec:
  source:
    worldSize: 128
    checkpointRoot: /checkpoints/job42
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: aevrel
        app.kubernetes.io/component: source
  target:
    worldSize: 64
    rootTemplate: /targets/job42/rank{rank}
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: aevrel
        app.kubernetes.io/component: target
`

func TestLoadMigrationAppliesProductDefaults(t *testing.T) {
	path := writeMigration(t, minimalMigration)
	migration, err := loadMigration(path, "team-a")
	if err != nil {
		t.Fatal(err)
	}
	if migration.Namespace != "team-a" {
		t.Fatalf("namespace = %q", migration.Namespace)
	}
	if migration.Spec.Checkpoint.Format != "dcp" {
		t.Fatalf("format = %q", migration.Spec.Checkpoint.Format)
	}
	if got := migration.Spec.Source.Mesh.Shape; len(got) != 1 || got[0] != 128 {
		t.Fatalf("source mesh = %v", got)
	}
	if got := migration.Spec.Target.Mesh.Shape; len(got) != 1 || got[0] != 64 {
		t.Fatalf("target mesh = %v", got)
	}
	if migration.Spec.Policy.MaxConcurrentTransfers != 16 {
		t.Fatalf("policy defaults missing: %#v", migration.Spec.Policy)
	}
	if migration.Spec.Artifacts.ClaimName != defaultArtifactClaim {
		t.Fatalf("artifact claim = %q", migration.Spec.Artifacts.ClaimName)
	}
}

func TestMigrateDryRunDoesNotNeedCluster(t *testing.T) {
	path := writeMigration(t, minimalMigration)
	var output bytes.Buffer
	err := execute(
		context.Background(),
		nil,
		"default",
		false,
		[]string{"migrate", "-f", path, "--dry-run"},
		&output,
	)
	if err != nil {
		t.Fatal(err)
	}
	text := output.String()
	for _, want := range []string{
		"kind: CheckpointMigration",
		"agentSelector:",
		"worldSize: 128",
		"formatVersion: 1.0.0",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("dry-run missing %q:\n%s", want, text)
		}
	}
}

func TestMigrateCreatesCheckpointMigration(t *testing.T) {
	path := writeMigration(t, minimalMigration)
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		Build()
	var output bytes.Buffer
	err := execute(
		context.Background(),
		kubeClient,
		"team-a",
		false,
		[]string{"migrate", "-f", path},
		&output,
	)
	if err != nil {
		t.Fatal(err)
	}
	var migration aevrelv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKey{Namespace: "team-a", Name: "job42"},
		&migration,
	); err != nil {
		t.Fatal(err)
	}
	if migration.Spec.Source.AgentSelector == nil {
		t.Fatal("agentSelector was not persisted")
	}
	if !strings.Contains(output.String(), "created migration team-a/job42") {
		t.Fatalf("unexpected output:\n%s", output.String())
	}
}

func TestPreflightAcceptsExplicitRankMap(t *testing.T) {
	// Preflight with explicit rank endpoints does not contact agents.
	const explicit = `
apiVersion: aevrel.dev/v1
kind: CheckpointMigration
metadata:
  name: preflight
spec:
  source:
    worldSize: 1
    checkpointRoot: /checkpoints/job
    rankMap:
      - rank: 0
        endpoint: http://source:9443
  target:
    worldSize: 1
    rootTemplate: /targets/job/rank{rank}
    rankMap:
      - rank: 0
        endpoint: http://target:9444
`
	path := writeMigration(t, explicit)
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	var output bytes.Buffer
	if err := execute(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"preflight", "-f", path},
		&output,
	); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "preflight ok") {
		t.Fatalf("unexpected preflight output:\n%s", output.String())
	}
}

func TestTargetReadyExampleIsValid(t *testing.T) {
	path := filepath.Join(
		"..",
		"..",
		"examples",
		"target-ready",
		"migration.yaml",
	)
	migration, err := loadMigration(path, "default")
	if err != nil {
		t.Fatal(err)
	}
	if migration.Spec.Target.Validation == nil {
		t.Fatal("example does not configure target validation")
	}
	if err := validateProductMigration(migration); err != nil {
		t.Fatal(err)
	}
}

func TestLiveRankSplitExampleIsValid(t *testing.T) {
	path := filepath.Join(
		"..",
		"..",
		"examples",
		"live-rank-split",
		"migration.yaml",
	)
	migration, err := loadMigration(path, "default")
	if err != nil {
		t.Fatal(err)
	}
	if migration.Spec.Mode != aevrelv1.MigrationModeLiveRankSplit {
		t.Fatalf("mode = %q", migration.Spec.Mode)
	}
	if migration.Spec.Live == nil ||
		migration.Spec.Live.MaxInFlight != 2 ||
		migration.Spec.Live.StateMode != aevrelv1.LiveStateModeL4 {
		t.Fatalf("live = %+v", migration.Spec.Live)
	}
	if migration.Spec.Live.Cutover.RequireStateComplete == nil ||
		!*migration.Spec.Live.Cutover.RequireStateComplete {
		t.Fatal("live.cutover.requireStateComplete was not defaulted")
	}
	if err := validateProductMigration(migration); err != nil {
		t.Fatal(err)
	}
	migration.Status.Phase = aevrelv1.MigrationPhaseTransferring
	migration.Status.Live = &aevrelv1.LiveRankSplitStatus{
		CutoverPhase: "following",
		InFlight:     []int32{2},
		Coverage:     []int32{0, 1, 2, 3},
		Quorum:       3,
	}
	var output bytes.Buffer
	if err := writeDescribe(&output, migration, true); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"Mode:       live-rank-split",
		"Live spec:  maxInFlight=2 stateMode=L4",
		"Live:       phase=following inFlight=[2]",
	} {
		if !strings.Contains(output.String(), want) {
			t.Fatalf("describe missing %q:\n%s", want, output.String())
		}
	}
}

func TestLiveRankSplitMaxInFlightOverride(t *testing.T) {
	path := filepath.Join(
		"..",
		"..",
		"examples",
		"live-rank-split",
		"migration.yaml",
	)
	var output bytes.Buffer
	err := executeMigrate(
		context.Background(),
		nil,
		"default",
		false,
		[]string{
			"-f",
			path,
			"--dry-run",
			"--max-in-flight",
			"3",
		},
		&output,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "maxInFlight: 3") {
		t.Fatalf("override missing:\n%s", output.String())
	}
}

func TestLiveRankSplitPlanIsProductSummary(t *testing.T) {
	path := filepath.Join(
		"..",
		"..",
		"examples",
		"live-rank-split",
		"migration.yaml",
	)
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	var output bytes.Buffer
	if err := execute(
		context.Background(),
		kubeClient,
		"default",
		false,
		[]string{"plan", "-f", path},
		&output,
	); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"Mode:           live-rank-split",
		"Handoff:        http://lrs-handoff.live.svc:9460",
		"Max in flight:  2",
		"Prereq:         executor must implement POST /v1/live/reconcile",
		"Fallback:       none",
		"Plan:           delegated to live executor",
	} {
		if !strings.Contains(output.String(), want) {
			t.Fatalf("plan missing %q:\n%s", want, output.String())
		}
	}
}

func TestParseGlobalAcceptsFlagsAfterCommand(t *testing.T) {
	namespace, jsonOut, args, err := parseGlobal(
		[]string{"migrate", "-f", "migration.yaml", "--json", "-n", "team-a"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if namespace != "team-a" || !jsonOut {
		t.Fatalf("namespace=%q json=%v", namespace, jsonOut)
	}
	if !reflect.DeepEqual(
		args,
		[]string{"migrate", "-f", "migration.yaml"},
	) {
		t.Fatalf("args = %v", args)
	}
}

func writeMigration(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "migration.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}
