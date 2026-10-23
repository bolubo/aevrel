package main

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func TestClusterAddWritesSecretAndCluster(t *testing.T) {
	path := filepath.Join(t.TempDir(), "clusters.yaml")
	var output bytes.Buffer
	err := execute(
		context.Background(),
		nil,
		"team-a",
		false,
		[]string{
			"cluster", "add", "source",
			"--endpoint", "https://gateway.example:9445",
			"--file", path,
		},
		&output,
	)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode := info.Mode().Perm(); mode != 0o600 {
		t.Fatalf("manifest mode = %o, want 600", mode)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	secret, cluster := decodeClusterAddManifest(t, data)
	if secret.Name != "source-gateway-auth" ||
		secret.Namespace != "team-a" {
		t.Fatalf("secret metadata = %s/%s", secret.Namespace, secret.Name)
	}
	if token := secret.StringData["token"]; len(token) != 43 {
		t.Fatalf("generated token length = %d, want 43", len(token))
	}
	if cluster.Name != "source" || cluster.Namespace != "team-a" {
		t.Fatalf("cluster metadata = %s/%s", cluster.Namespace, cluster.Name)
	}
	if cluster.Spec.Role != "both" {
		t.Fatalf("default role = %q, want both", cluster.Spec.Role)
	}
	if cluster.Spec.Endpoint != "https://gateway.example:9445" {
		t.Fatalf("endpoint = %q", cluster.Spec.Endpoint)
	}
	if cluster.Spec.SecretRef.Name != "source-gateway-auth" ||
		cluster.Spec.SecretRef.Key != "token" {
		t.Fatalf("secretRef = %#v", cluster.Spec.SecretRef)
	}
	if !strings.Contains(output.String(), "wrote cluster onboarding manifest") {
		t.Fatalf("unexpected output:\n%s", output.String())
	}
}

func TestClusterAddJSONOutputsKubernetesList(t *testing.T) {
	var output bytes.Buffer
	err := execute(
		context.Background(),
		nil,
		"aevrel-control",
		true,
		[]string{
			"cluster", "add", "target",
			"--endpoint", "http://target-gateway:9445",
			"--role", "target",
			"--secret", "target-auth",
			"--token", "test-token",
		},
		&output,
	)
	if err != nil {
		t.Fatal(err)
	}
	var list struct {
		APIVersion string            `json:"apiVersion"`
		Kind       string            `json:"kind"`
		Items      []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(output.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	if list.APIVersion != "v1" || list.Kind != "List" || len(list.Items) != 2 {
		t.Fatalf("unexpected list envelope: %#v", list)
	}
	var secret corev1.Secret
	if err := json.Unmarshal(list.Items[0], &secret); err != nil {
		t.Fatal(err)
	}
	var cluster aevrelv1.AevrelCluster
	if err := json.Unmarshal(list.Items[1], &cluster); err != nil {
		t.Fatal(err)
	}
	if secret.StringData["token"] != "test-token" {
		t.Fatalf("token = %q", secret.StringData["token"])
	}
	if cluster.Spec.Role != "target" ||
		cluster.Spec.SecretRef.Name != "target-auth" {
		t.Fatalf("unexpected cluster: %#v", cluster.Spec)
	}
}

func TestClusterAddRejectsInvalidConfiguration(t *testing.T) {
	tests := []struct {
		name string
		args []string
		want string
	}{
		{
			name: "missing endpoint",
			args: []string{"source"},
			want: "--endpoint is required",
		},
		{
			name: "unsupported scheme",
			args: []string{
				"source", "--endpoint", "ftp://gateway:9445",
			},
			want: "http:// or https://",
		},
		{
			name: "missing scheme",
			args: []string{
				"source", "--endpoint", "gateway:9445",
			},
			want: "http:// or https://",
		},
		{
			name: "credentials in endpoint",
			args: []string{
				"source", "--endpoint", "https://user:pass@gateway:9445",
			},
			want: "without credentials",
		},
		{
			name: "invalid role",
			args: []string{
				"source", "--endpoint", "https://gateway:9445",
				"--role", "worker",
			},
			want: "--role must be source, target, or both",
		},
		{
			name: "invalid cluster name",
			args: []string{
				"Source_1", "--endpoint", "https://gateway:9445",
			},
			want: "invalid cluster name",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var output bytes.Buffer
			err := executeClusterAdd(false, "default", test.args, &output)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestClusterAddRefusesToOverwrite(t *testing.T) {
	path := filepath.Join(t.TempDir(), "clusters.yaml")
	const original = "do not replace\n"
	if err := os.WriteFile(path, []byte(original), 0o600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	err := executeClusterAdd(
		false,
		"default",
		[]string{
			"source", "--endpoint", "https://gateway:9445",
			"--file", path,
		},
		&output,
	)
	if err == nil || !strings.Contains(err.Error(), "without overwriting") {
		t.Fatalf("error = %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != original {
		t.Fatalf("file changed to:\n%s", data)
	}
}

func TestClusterAddDoesNotNeedKubeClient(t *testing.T) {
	if needsKubeClient([]string{"cluster", "add", "source"}) {
		t.Fatal("cluster add unexpectedly requires a Kubernetes client")
	}
}

func decodeClusterAddManifest(
	t *testing.T,
	data []byte,
) (corev1.Secret, aevrelv1.AevrelCluster) {
	t.Helper()
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096)
	var secret corev1.Secret
	if err := decoder.Decode(&secret); err != nil {
		t.Fatal(err)
	}
	var cluster aevrelv1.AevrelCluster
	if err := decoder.Decode(&cluster); err != nil {
		t.Fatal(err)
	}
	return secret, cluster
}
