package gateway

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/clusterclient"
)

func TestGatewayResolveAndProxySourceAgent(t *testing.T) {
	agent := newTestServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer agent-token" {
			http.Error(w, "bad token", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/v1/ranks":
			_, _ = io.WriteString(w, `{"version":"1","ranks":[0]}`)
		case "/v1/inventory":
			_, _ = io.WriteString(w, `{"version":"1","rank":0,"format":"dcp"}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer agent.Close()

	agentURL, err := url.Parse(agent.URL)
	if err != nil {
		t.Fatal(err)
	}
	host, port, err := netSplitHostPort(agentURL)
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(&corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Namespace: "default",
				Name:      "pod-a",
				Labels:    map[string]string{"app": "shardferry"},
			},
			Status: corev1.PodStatus{
				Phase: corev1.PodRunning,
				PodIP: host,
			},
		}).
		Build()
	server, err := New(Config{
		PublicURL:    "http://gateway.example",
		ClusterName:  "source",
		GatewayToken: "gateway-token",
		AgentToken:   "agent-token",
		AgentClient:  directAgentClient("agent-token"),
		Client:       kube,
	})
	if err != nil {
		t.Fatal(err)
	}
	handler := server.Handler()
	resolveBody, _ := json.Marshal(clusterclient.ResolveRequest{
		Role:        "source",
		Namespace:   "default",
		MatchLabels: map[string]string{"app": "shardferry"},
		Scheme:      "http",
		Port:        port,
		WorldSize:   1,
	})
	resolveRequest := httptest.NewRequest(
		http.MethodPost,
		"/v1/resolve",
		bytes.NewReader(resolveBody),
	)
	resolveRequest.Header.Set("Authorization", "Bearer gateway-token")
	resolveResponse := httptest.NewRecorder()
	handler.ServeHTTP(resolveResponse, resolveRequest)
	if resolveResponse.Code != http.StatusOK {
		t.Fatalf("resolve status = %d: %s", resolveResponse.Code, resolveResponse.Body.String())
	}
	var resolved clusterclient.ResolveResponse
	if err := json.Unmarshal(resolveResponse.Body.Bytes(), &resolved); err != nil {
		t.Fatal(err)
	}
	if got := resolved.Ranks[0].Endpoint; got != "http://gateway.example/v1/agents/pod-a" {
		t.Fatalf("endpoint = %q", got)
	}

	proxyRequest := httptest.NewRequest(
		http.MethodGet,
		"/v1/agents/pod-a/v1/inventory?rank=0",
		nil,
	)
	proxyRequest.Header.Set("Authorization", "Bearer gateway-token")
	proxyResponse := httptest.NewRecorder()
	handler.ServeHTTP(proxyResponse, proxyRequest)
	if proxyResponse.Code != http.StatusOK {
		t.Fatalf("proxy status = %d: %s", proxyResponse.Code, proxyResponse.Body.String())
	}
	if !bytes.Contains(proxyResponse.Body.Bytes(), []byte(`"format":"dcp"`)) {
		t.Fatalf("proxy body = %s", proxyResponse.Body.String())
	}
}

func TestGatewayInfoReportsVersion(t *testing.T) {
	server, err := New(Config{
		PublicURL:    "http://gateway.example",
		ClusterName:  "source",
		Version:      "0.2.0",
		GatewayToken: "gateway-token",
		AgentToken:   "agent-token",
		AgentClient:  directAgentClient("agent-token"),
		Client:       fake.NewClientBuilder().Build(),
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/v1/info", nil)
	request.Header.Set("Authorization", "Bearer gateway-token")
	response := httptest.NewRecorder()
	server.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d: %s", response.Code, response.Body.String())
	}
	var info clusterclient.GatewayInfo
	if err := json.Unmarshal(response.Body.Bytes(), &info); err != nil {
		t.Fatal(err)
	}
	if info.GatewayVersion != "0.2.0" || info.Cluster != "source" {
		t.Fatalf("info = %#v", info)
	}
}

func directAgentClient(token string) *agentclient.Client {
	client := agentclient.New(token)
	client.HTTP.Transport = &http.Transport{Proxy: nil}
	return client
}

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

func localIP() string {
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		panic(err)
	}
	defer connection.Close()
	return connection.LocalAddr().(*net.UDPAddr).IP.String()
}

func netSplitHostPort(value *url.URL) (string, int32, error) {
	host := value.Hostname()
	port, err := strconv.Atoi(value.Port())
	if err != nil {
		return "", 0, err
	}
	return host, int32(port), nil
}
