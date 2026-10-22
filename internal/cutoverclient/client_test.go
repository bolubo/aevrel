package cutoverclient

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestClientCallsCutoverServiceSurface(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/status":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"state":         "following",
				"source_latest": "step-1",
			})
		case "/v1/follow":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"results": []map[string]any{{"version_id": "step-1"}},
			})
		case "/v1/cutover":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"state":      "promoted",
				"version_id": "step-1",
			})
		case "/v1/recover":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"state": "promoted",
			})
		default:
			http.NotFound(w, r)
		}
	}))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	server.Listener.Close()
	server.Listener = listener
	server.Start()
	defer server.Close()

	port := server.Listener.Addr().(*net.TCPAddr).Port
	client := New(fmt.Sprintf("http://%s:%d", localIP(t), port), "secret")
	status, err := client.Status(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if status["state"] != "following" {
		t.Fatalf("status = %v", status)
	}
	results, err := client.Follow(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 1 {
		t.Fatalf("follow results = %v", results)
	}
	cutover, err := client.Cutover(context.Background(), "step-1")
	if err != nil {
		t.Fatal(err)
	}
	if cutover["state"] != "promoted" {
		t.Fatalf("cutover = %v", cutover)
	}
	recovered, err := client.Recover(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if recovered["state"] != "promoted" {
		t.Fatalf("recover = %v", recovered)
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
