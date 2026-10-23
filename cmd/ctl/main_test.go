package main

import "testing"

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
