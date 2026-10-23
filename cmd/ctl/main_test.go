package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestNeedsKubeClientForOfflineCommands(t *testing.T) {
	if needsKubeClient([]string{
		"migrate",
		"-f",
		"migration.yaml",
		"--dry-run",
	}) {
		t.Fatal("migrate --dry-run must not require a Kubernetes client")
	}
	if !needsKubeClient([]string{"plan", "-f", "migration.yaml"}) {
		t.Fatal("plan must require a Kubernetes client")
	}
}

func TestPlanDryRunReturnsOfflineValidationHint(t *testing.T) {
	_, err := parsePlan([]string{"--dry-run"})
	if err == nil {
		t.Fatal("parsePlan(--dry-run) should fail")
	}
	if !strings.Contains(err.Error(), "shardferryctl migrate -f FILE --dry-run") {
		t.Fatalf("error should point to offline migrate validation, got: %v", err)
	}
}

func TestMainMissingKubeconfigPrintsOfflineValidationHint(t *testing.T) {
	const childEnv = "SHARDFERRY_TEST_MAIN_MISSING_KUBECONFIG"
	if os.Getenv(childEnv) == "1" {
		os.Args = []string{"shardferryctl", "plan", "-f", "migration.yaml"}
		main()
		return
	}

	var stderr bytes.Buffer
	cmd := exec.Command(os.Args[0], "-test.run=^TestMainMissingKubeconfigPrintsOfflineValidationHint$")
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "KUBECONFIG=") &&
			!strings.HasPrefix(entry, childEnv+"=") {
			cmd.Env = append(cmd.Env, entry)
		}
	}
	cmd.Env = append(
		cmd.Env,
		"KUBECONFIG="+filepath.Join(t.TempDir(), "missing-kubeconfig"),
		childEnv+"=1",
	)
	cmd.Stderr = &stderr

	err := cmd.Run()
	exitErr, ok := err.(*exec.ExitError)
	if !ok || exitErr.ExitCode() != 1 {
		t.Fatalf("child exit = %v, want exit code 1; stderr: %s", err, stderr.String())
	}

	for _, want := range []string{
		"requires a Kubernetes kubeconfig",
		"shardferryctl migrate -f FILE --dry-run",
	} {
		if !strings.Contains(stderr.String(), want) {
			t.Fatalf("stderr missing %q:\n%s", want, stderr.String())
		}
	}
}
