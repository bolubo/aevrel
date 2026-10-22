package agentclient

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(handler http.Handler) *httptest.Server {
	listener, err := net.Listen("tcp", localIP()+":0")
	if err != nil {
		panic(err)
	}
	server := httptest.NewUnstartedServer(handler)
	server.Listener = listener
	server.Start()
	return server
}

func TestEndpointTokenOverridesGlobalToken(t *testing.T) {
	client := New("global")
	client.RegisterEndpointToken("https://gateway.example", "cluster")
	if got := client.tokenFor("https://gateway.example/v1/agents/source"); got != "cluster" {
		t.Fatalf("endpoint token = %q, want cluster", got)
	}
	if got := client.tokenFor("https://other.example/v1/ranks"); got != "global" {
		t.Fatalf("fallback token = %q, want global", got)
	}
}

func localIP() string {
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		panic(err)
	}
	defer connection.Close()
	return connection.LocalAddr().(*net.UDPAddr).IP.String()
}

func TestInventoryUsesBearerTokenAndDecodesContract(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer secret" {
			t.Errorf("authorization = %q", got)
			http.Error(w, "bad auth", http.StatusUnauthorized)
			return
		}
		if r.URL.Path != "/v1/inventory" || r.URL.Query().Get("rank") != "7" {
			t.Errorf("unexpected request: %s", r.URL.String())
			http.Error(w, "unexpected request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"version":"1",
			"rank":7,
			"format":"dcp",
			"metadata_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"tensors":[{"name":"w","shape":[8,4],"dtype":"float32"}],
			"chunks":[{
				"id":"sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
				"tensor":"w","rank":7,"file":"job42/__7_0.distcp",
				"tensor_offset":[0,0],"tensor_size":[1,4],
				"byte_ranges":[[0,16]],
				"checksum":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
			}],
			"agent_version":"1.0.0",
			"torch_version":"2.14.1"
		}`))
	}))
	defer server.Close()

	client := New("secret")
	inventory, err := client.Inventory(context.Background(), server.URL, 7)
	if err != nil {
		t.Fatalf("inventory: %v", err)
	}
	if inventory.Rank != 7 || len(inventory.Tensors) != 1 || len(inventory.Chunks) != 1 {
		t.Fatalf("unexpected inventory: %+v", inventory)
	}
	if inventory.Tensors[0].Name != "w" ||
		inventory.Chunks[0].Checksum == "" ||
		inventory.Chunks[0].ID == "" {
		t.Fatalf("contract decode failed: %+v", inventory)
	}
}

func TestMetadataTransferUsesBearerTokenAndDigest(t *testing.T) {
	const metadata = "small-dcp-metadata"
	sum := sha256.Sum256([]byte(metadata))
	digest := "sha256:" + hex.EncodeToString(sum[:])
	var installed []byte
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer secret" {
			t.Errorf("authorization = %q", got)
			http.Error(w, "bad auth", http.StatusUnauthorized)
			return
		}
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/metadata":
			w.Header().Set("Content-Type", "application/octet-stream")
			w.Header().Set("X-Metadata-SHA256", digest)
			_, _ = w.Write([]byte(metadata))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/metadata":
			data, err := io.ReadAll(r.Body)
			if err != nil {
				t.Fatal(err)
			}
			installed = data
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(
				`{"ok":true,"metadata_hash":"` + digest + `","bytes":18}`,
			))
		default:
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
			http.Error(w, "unexpected request", http.StatusBadRequest)
		}
	}))
	defer server.Close()

	client := New("secret")
	data, gotDigest, err := client.Metadata(context.Background(), server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != metadata || gotDigest != digest {
		t.Fatalf("metadata = %q digest=%q", data, gotDigest)
	}
	installedDigest, err := client.PutMetadata(context.Background(), server.URL, data)
	if err != nil {
		t.Fatal(err)
	}
	if installedDigest != digest || string(installed) != metadata {
		t.Fatalf(
			"installed digest=%q bytes=%q",
			installedDigest,
			installed,
		)
	}
}

func TestRanksDecodesLocalRankDiscovery(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/v1/ranks" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
			http.Error(w, "unexpected request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"version":"1","ranks":[0,2,4]}`))
	}))
	defer server.Close()

	ranks, err := New("secret").Ranks(context.Background(), server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if len(ranks) != 3 || ranks[0] != 0 || ranks[1] != 2 || ranks[2] != 4 {
		t.Fatalf("unexpected ranks: %v", ranks)
	}
}

func TestPlanPostsSpecAndDecodesPlan(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/plan" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
			http.Error(w, "unexpected request", http.StatusBadRequest)
			return
		}
		var body struct {
			Version string         `json:"version"`
			Spec    map[string]any `json:"spec"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Version != "1" || body.Spec["version"] != "1" {
			t.Errorf("unexpected plan request: %+v", body)
			http.Error(w, "unexpected plan request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"version":"1","plan":{"version":"1","nodes":[]}}`))
	}))
	defer server.Close()

	client := New("secret")
	response, err := client.Plan(
		context.Background(),
		server.URL,
		map[string]any{"version": "1"},
	)
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	if response.Version != "1" || response.Plan["version"] != "1" {
		t.Fatalf("unexpected plan response: %+v", response)
	}
}

func TestClientSurfacesAgentErrorEnvelope(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{
			"version":"1","code":"PERMISSION_DENIED","category":"control",
			"retryable":false,"phase":"request","rank":0,
			"details":"missing token"
		}`))
	}))
	defer server.Close()

	client := New("wrong")
	_, err := client.Inventory(context.Background(), server.URL, 0)
	if err == nil {
		t.Fatal("expected an agent error")
	}
	var agentErr *AgentError
	if !errors.As(err, &agentErr) {
		t.Fatalf("error type = %T, want *AgentError", err)
	}
	if agentErr.Code != "PERMISSION_DENIED" ||
		agentErr.Category != "control" ||
		agentErr.Phase != "request" ||
		agentErr.IsRetryable() {
		t.Fatalf("unexpected agent error: %+v", agentErr)
	}
}

func TestAgentErrorRetryableFallsBackToHTTPStatus(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("temporarily unavailable"))
	}))
	defer server.Close()

	client := New("secret")
	_, err := client.Inventory(context.Background(), server.URL, 0)
	if err == nil {
		t.Fatal("expected an agent error")
	}
	var agentErr *AgentError
	if !errors.As(err, &agentErr) {
		t.Fatalf("error type = %T, want *AgentError", err)
	}
	if !agentErr.IsRetryable() {
		t.Fatalf("503 should be retryable: %+v", agentErr)
	}
	if !Retryable(err) {
		t.Fatal("Retryable did not classify 503")
	}
}

func TestCreateAndAdvanceTask(t *testing.T) {
	server := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		state := "created"
		switch r.URL.Path {
		case "/v1/tasks":
			var task Task
			if err := json.NewDecoder(r.Body).Decode(&task); err != nil {
				t.Fatal(err)
			}
			if task.SpecRef != "spec" || task.PlanRef != "plan" {
				t.Fatalf("unexpected task: %+v", task)
			}
		case "/v1/tasks/task-1/prepare":
			state = "prepared"
		case "/v1/tasks/task-1/execute":
			state = "staged"
		case "/v1/tasks/task-1/commit":
			state = "committed"
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"task_id":"task-1","version":"1","migration_uid":"m-1",
			"rank":0,"task_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"target_root":"/target/rank0","spec_ref":"spec","plan_ref":"plan",
			"plan_slice":{},"state":"` + state + `"
		}`))
	}))
	defer server.Close()

	client := New("secret")
	task, err := client.CreateTask(context.Background(), server.URL, Task{
		Version:      "1",
		MigrationUID: "m-1",
		Rank:         0,
		TaskHash:     "sha256:" + strings.Repeat("a", 64),
		TargetRoot:   "/target/rank0",
		SpecRef:      "spec",
		PlanRef:      "plan",
		PlanSlice:    map[string]any{},
		State:        "created",
	})
	if err != nil {
		t.Fatal(err)
	}
	if task.TaskID != "task-1" {
		t.Fatalf("task id = %q", task.TaskID)
	}
	task, err = client.PrepareTask(context.Background(), server.URL, task.TaskID)
	if err != nil || task.State != "prepared" {
		t.Fatalf("prepare: state=%q err=%v", task.State, err)
	}
	task, err = client.ExecuteTask(context.Background(), server.URL, task.TaskID)
	if err != nil || task.State != "staged" {
		t.Fatalf("execute: state=%q err=%v", task.State, err)
	}
	task, err = client.CommitTask(context.Background(), server.URL, task.TaskID)
	if err != nil || task.State != "committed" {
		t.Fatalf("commit: state=%q err=%v", task.State, err)
	}
}
