package liveclient

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func TestClientObservesLiveRankSplitContract(t *testing.T) {
	var received struct {
		APIVersion string `json:"apiVersion"`
		Kind       string `json:"kind"`
		Migration  struct {
			Namespace  string `json:"namespace"`
			Name       string `json:"name"`
			Generation int64  `json:"generation"`
		} `json:"migration"`
		Spec struct {
			Mode string `json:"mode"`
			Live struct {
				MaxInFlight int32  `json:"maxInFlight"`
				StateMode   string `json:"stateMode"`
			} `json:"live"`
		} `json:"spec"`
		PreviousStatus *aevrelv1.LiveRankSplitStatus `json:"previousStatus"`
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				http.Error(w, "method", http.StatusMethodNotAllowed)
				return
			}
			if r.URL.Path != reconcilePath {
				http.NotFound(w, r)
				return
			}
			if r.Header.Get("Authorization") != "Bearer live-secret" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"phase":         "succeeded",
				"inFlight":      []int32{},
				"coverage":      []int32{0, 1, 2, 3},
				"quorum":        3,
				"stateComplete": true,
				"stateRoot":     "/state/step-100/rank{rank}",
				"proofRef":      "https://artifacts/live-proof.json",
				"message":       "all ranks joined",
				"ranks": []map[string]any{{
					"rank":  0,
					"phase": "active",
					"owner": 1,
					"epoch": 2,
				}},
			})
		},
	))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	defer server.Close()

	port := server.Listener.Addr().(*net.TCPAddr).Port
	baseURL := fmt.Sprintf("http://%s:%d", localIP(t), port)
	migration := liveMigration(baseURL)
	client := New("http://127.0.0.1:1", "live-secret")
	observation, err := client.Observe(context.Background(), migration)
	if err != nil {
		t.Fatal(err)
	}
	if observation.Phase != "succeeded" ||
		!observation.StateComplete ||
		observation.Quorum != 3 ||
		observation.ProofRef == "" {
		t.Fatalf("observation = %+v", observation)
	}
	if received.APIVersion != "aevrel.dev/v1" ||
		received.Kind != "LiveRankSplitReconcile" ||
		received.Migration.Namespace != "default" ||
		received.Migration.Name != "live-demo" ||
		received.Migration.Generation != 4 {
		t.Fatalf("request identity = %+v", received)
	}
	if received.Spec.Mode != aevrelv1.MigrationModeLiveRankSplit ||
		received.Spec.Live.MaxInFlight != 1 ||
		received.Spec.Live.StateMode != aevrelv1.LiveStateModeL4 {
		t.Fatalf("request spec = %+v", received.Spec)
	}
	if received.PreviousStatus == nil ||
		received.PreviousStatus.CutoverPhase != "following" {
		t.Fatalf("previous status = %+v", received.PreviousStatus)
	}
}

func TestClientRequiresHandoffEndpoint(t *testing.T) {
	migration := liveMigration("")
	client := New("", "live-secret")
	_, err := client.Observe(context.Background(), migration)
	if err == nil || !strings.Contains(err.Error(), "handoffEndpoint") {
		t.Fatalf("error = %v, want missing handoffEndpoint", err)
	}
}

func TestClientRejectsServiceFailure(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "quorum unavailable", http.StatusServiceUnavailable)
		},
	))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	defer server.Close()

	port := server.Listener.Addr().(*net.TCPAddr).Port
	baseURL := fmt.Sprintf("http://%s:%d", localIP(t), port)
	client := New("", "live-secret")
	_, err = client.Observe(
		context.Background(),
		liveMigration(baseURL),
	)
	if err == nil || !strings.Contains(err.Error(), "quorum unavailable") {
		t.Fatalf("error = %v, want service failure", err)
	}
}

func liveMigration(endpoint string) *aevrelv1.CheckpointMigration {
	return &aevrelv1.CheckpointMigration{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:  "default",
			Name:       "live-demo",
			Generation: 4,
		},
		Spec: aevrelv1.CheckpointMigrationSpec{
			Mode: aevrelv1.MigrationModeLiveRankSplit,
			Live: &aevrelv1.LiveRankSplitSpec{
				MaxInFlight:     1,
				StateMode:       aevrelv1.LiveStateModeL4,
				HandoffEndpoint: endpoint,
			},
		},
		Status: aevrelv1.CheckpointMigrationStatus{
			Live: &aevrelv1.LiveRankSplitStatus{
				CutoverPhase: "following",
			},
		},
	}
}

func localIP(t *testing.T) string {
	t.Helper()
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	return connection.LocalAddr().(*net.UDPAddr).IP.String()
}
