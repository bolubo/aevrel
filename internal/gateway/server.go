package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httputil"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/clusterclient"
)

var agentPath = regexp.MustCompile(`^/v1/agents/([^/]+)(/.*)?$`)

type Config struct {
	PublicURL         string
	ClusterName       string
	Version           string
	GatewayToken      string
	AgentToken        string
	RequireClientCert bool
	AgentClient       *agentclient.Client
	AgentTransport    http.RoundTripper
	Client            client.Client
	Namespace         string
}

type Server struct {
	config Config
	mu     sync.RWMutex
	agents map[string]string
}

func New(config Config) (*Server, error) {
	if config.PublicURL == "" {
		return nil, fmt.Errorf("gateway public URL is required")
	}
	if config.AgentClient == nil {
		return nil, fmt.Errorf("gateway agent client is required")
	}
	if config.Client == nil {
		return nil, fmt.Errorf("gateway Kubernetes client is required")
	}
	if config.GatewayToken == "" {
		return nil, fmt.Errorf("gateway token is required")
	}
	if config.AgentToken == "" {
		return nil, fmt.Errorf("agent token is required")
	}
	if config.Version == "" {
		config.Version = "dev"
	}
	return &Server{
		config: config,
		agents: make(map[string]string),
	}, nil
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/readyz", s.health)
	mux.HandleFunc("/v1/info", s.authorized(s.info, false))
	mux.HandleFunc("/v1/cluster", s.authorized(s.clusterInfo, false))
	mux.HandleFunc("/v1/resolve", s.authorized(s.resolve, false))
	mux.HandleFunc("/v1/agents/", s.authorized(s.proxy, true))
	return mux
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *Server) info(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, clusterclient.GatewayInfo{
		Version:        "1",
		Cluster:        s.config.ClusterName,
		GatewayVersion: s.config.Version,
	})
}

func (s *Server) clusterInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{
		"version": "1",
		"cluster": s.config.ClusterName,
		"ready":   true,
	})
}

func (s *Server) authorized(next http.HandlerFunc, allowAgentToken bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.config.RequireClientCert &&
			(r.TLS == nil || len(r.TLS.PeerCertificates) == 0) {
			http.Error(w, "client certificate required", http.StatusUnauthorized)
			return
		}
		got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if got != s.config.GatewayToken &&
			!(allowAgentToken &&
				strings.Contains(r.URL.Path, "/v1/shards/") &&
				got == s.config.AgentToken) {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func (s *Server) resolve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var request clusterclient.ResolveRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		http.Error(w, "invalid request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if request.WorldSize < 1 {
		http.Error(w, "world_size must be positive", http.StatusBadRequest)
		return
	}
	if len(request.MatchLabels) == 0 {
		http.Error(w, "match_labels is required", http.StatusBadRequest)
		return
	}
	if request.Role != "source" && request.Role != "target" {
		http.Error(w, "role must be source or target", http.StatusBadRequest)
		return
	}
	namespace := request.Namespace
	if namespace == "" {
		namespace = s.config.Namespace
	}
	if namespace == "" {
		namespace = "default"
	}
	scheme := request.Scheme
	if scheme == "" {
		scheme = "http"
	}
	port := request.Port
	if port == 0 {
		port = 9443
	}
	pods, err := s.readyPods(r.Context(), namespace, request.MatchLabels)
	if err != nil {
		http.Error(w, err.Error(), http.StatusServiceUnavailable)
		return
	}
	agents := make(map[string]string, len(pods))
	for _, pod := range pods {
		agents[pod.Name] = scheme + "://" + pod.Status.PodIP + ":" + strconv.Itoa(int(port))
	}
	var ranks []clusterclient.RankEndpoint
	if request.Role == "source" {
		ranks, err = s.sourceRanks(r.Context(), pods, agents, request.WorldSize)
	} else {
		ranks, err = s.targetRanks(pods, agents, request.WorldSize)
	}
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	s.mu.Lock()
	for id, endpoint := range agents {
		s.agents[id] = endpoint
	}
	s.mu.Unlock()
	writeJSON(w, clusterclient.ResolveResponse{
		Version: "1",
		Cluster: s.config.ClusterName,
		Ranks:   ranks,
	})
}

func (s *Server) sourceRanks(
	ctx context.Context,
	pods []corev1.Pod,
	agents map[string]string,
	worldSize int32,
) ([]clusterclient.RankEndpoint, error) {
	assigned := make(map[int32]clusterclient.RankEndpoint, worldSize)
	for _, pod := range pods {
		endpoint := agents[pod.Name]
		ranks, err := s.config.AgentClient.Ranks(ctx, endpoint)
		if err != nil {
			return nil, fmt.Errorf("agent %s ranks: %w", pod.Name, err)
		}
		for _, rank := range ranks {
			if rank < 0 || rank >= worldSize {
				return nil, fmt.Errorf(
					"agent %s reports out-of-range rank %d",
					pod.Name,
					rank,
				)
			}
			if previous, ok := assigned[rank]; ok {
				return nil, fmt.Errorf(
					"rank %d is served by both %s and %s",
					rank,
					previous.Agent,
					pod.Name,
				)
			}
			assigned[rank] = clusterclient.RankEndpoint{
				Rank:     rank,
				Agent:    pod.Name,
				Endpoint: s.proxyEndpoint(pod.Name),
			}
		}
	}
	result := make([]clusterclient.RankEndpoint, 0, worldSize)
	for rank := int32(0); rank < worldSize; rank++ {
		entry, ok := assigned[rank]
		if !ok {
			return nil, fmt.Errorf("source rank %d has no agent", rank)
		}
		result = append(result, entry)
	}
	return result, nil
}

func (s *Server) targetRanks(
	pods []corev1.Pod,
	agents map[string]string,
	worldSize int32,
) ([]clusterclient.RankEndpoint, error) {
	if len(pods) == 0 {
		return nil, fmt.Errorf("no target agents are ready")
	}
	result := make([]clusterclient.RankEndpoint, 0, worldSize)
	for rank := int32(0); rank < worldSize; rank++ {
		pod := pods[int(rank)%len(pods)]
		if agents[pod.Name] == "" {
			return nil, fmt.Errorf("target agent %s has no endpoint", pod.Name)
		}
		result = append(result, clusterclient.RankEndpoint{
			Rank:     rank,
			Agent:    pod.Name,
			Endpoint: s.proxyEndpoint(pod.Name),
		})
	}
	return result, nil
}

func (s *Server) readyPods(
	ctx context.Context,
	namespace string,
	labels map[string]string,
) ([]corev1.Pod, error) {
	var pods corev1.PodList
	if err := s.config.Client.List(
		ctx,
		&pods,
		client.InNamespace(namespace),
		client.MatchingLabels(labels),
	); err != nil {
		return nil, fmt.Errorf("list agents in namespace %s: %w", namespace, err)
	}
	ready := make([]corev1.Pod, 0, len(pods.Items))
	for _, pod := range pods.Items {
		if pod.DeletionTimestamp != nil || pod.Status.PodIP == "" {
			continue
		}
		if pod.Status.Phase != corev1.PodRunning {
			continue
		}
		if !podReady(pod) {
			continue
		}
		ready = append(ready, pod)
	}
	sort.Slice(ready, func(i, j int) bool {
		return ready[i].Name < ready[j].Name
	})
	if len(ready) == 0 {
		return nil, fmt.Errorf("no ready agent pods match %v in namespace %s", labels, namespace)
	}
	return ready, nil
}

func (s *Server) proxyEndpoint(agentName string) string {
	return strings.TrimRight(s.config.PublicURL, "/") +
		"/v1/agents/" + url.PathEscape(agentName)
}

func (s *Server) proxy(w http.ResponseWriter, r *http.Request) {
	match := agentPath.FindStringSubmatch(r.URL.Path)
	if len(match) < 3 {
		http.NotFound(w, r)
		return
	}
	agentName, err := url.PathUnescape(match[1])
	if err != nil {
		http.Error(w, "invalid agent name", http.StatusBadRequest)
		return
	}
	s.mu.RLock()
	target, ok := s.agents[agentName]
	s.mu.RUnlock()
	if !ok {
		http.Error(w, "unknown agent", http.StatusNotFound)
		return
	}
	targetURL, err := url.Parse(target)
	if err != nil {
		http.Error(w, "invalid registered agent", http.StatusBadGateway)
		return
	}
	rest := match[2]
	if rest == "" {
		rest = "/"
	}
	proxy := httputil.NewSingleHostReverseProxy(targetURL)
	if s.config.AgentTransport != nil {
		proxy.Transport = s.config.AgentTransport
	}
	proxy.Director = func(request *http.Request) {
		request.URL.Scheme = targetURL.Scheme
		request.URL.Host = targetURL.Host
		request.URL.Path = rest
		request.URL.RawQuery = r.URL.RawQuery
		request.Host = targetURL.Host
		request.Header.Set("Authorization", "Bearer "+s.config.AgentToken)
	}
	proxy.ServeHTTP(w, r)
}

func podReady(pod corev1.Pod) bool {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return true
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(value)
}
