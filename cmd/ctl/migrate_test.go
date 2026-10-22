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

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

const minimalMigration = `
apiVersion: shardferry.io/v1
kind: CheckpointMigration
metadata:
  name: job42
spec:
  source:
    worldSize: 128
    checkpointRoot: /checkpoints/job42
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: shardferry
        app.kubernetes.io/component: source
  target:
    worldSize: 64
    rootTemplate: /targets/job42/rank{rank}
    agentSelector:
      matchLabels:
        app.kubernetes.io/name: shardferry
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
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointMigration{}).
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
	var migration shardferryv1.CheckpointMigration
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
