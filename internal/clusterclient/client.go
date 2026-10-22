package clusterclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type ResolveRequest struct {
	Version     string            `json:"version"`
	Role        string            `json:"role"`
	Namespace   string            `json:"namespace,omitempty"`
	MatchLabels map[string]string `json:"match_labels,omitempty"`
	Port        int32             `json:"port,omitempty"`
	Scheme      string            `json:"scheme,omitempty"`
	WorldSize   int32             `json:"world_size"`
}

type RankEndpoint struct {
	Rank     int32  `json:"rank"`
	Agent    string `json:"agent,omitempty"`
	Endpoint string `json:"endpoint"`
}

type ResolveResponse struct {
	Version string         `json:"version"`
	Cluster string         `json:"cluster,omitempty"`
	Ranks   []RankEndpoint `json:"ranks"`
}

type GatewayInfo struct {
	Version        string `json:"version"`
	Cluster        string `json:"cluster,omitempty"`
	GatewayVersion string `json:"gateway_version,omitempty"`
}

type HealthResponse struct {
	Version string `json:"version"`
	Cluster string `json:"cluster,omitempty"`
	Ready   bool   `json:"ready"`
}

type Client struct {
	HTTP  *http.Client
	Token string
}

func New(token string) *Client {
	return &Client{
		HTTP:  &http.Client{Timeout: 30 * time.Second},
		Token: token,
	}
}

func (c *Client) Resolve(
	ctx context.Context,
	endpoint string,
	token string,
	request ResolveRequest,
) (ResolveResponse, error) {
	if request.Version == "" {
		request.Version = "1"
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return ResolveResponse{}, fmt.Errorf("encode resolve request: %w", err)
	}
	httpRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		strings.TrimRight(endpoint, "/")+"/v1/resolve",
		bytes.NewReader(payload),
	)
	if err != nil {
		return ResolveResponse{}, err
	}
	if token == "" {
		token = c.Token
	}
	httpRequest.Header.Set("Authorization", "Bearer "+token)
	httpRequest.Header.Set("Content-Type", "application/json")
	response, err := c.HTTP.Do(httpRequest)
	if err != nil {
		return ResolveResponse{}, fmt.Errorf("resolve agents via %s: %w", endpoint, err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return ResolveResponse{}, fmt.Errorf("read resolve response: %w", err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return ResolveResponse{}, fmt.Errorf(
			"resolve agents via %s: HTTP %d: %s",
			endpoint,
			response.StatusCode,
			strings.TrimSpace(string(body)),
		)
	}
	var result ResolveResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return ResolveResponse{}, fmt.Errorf("decode resolve response: %w", err)
	}
	if len(result.Ranks) == 0 {
		return ResolveResponse{}, fmt.Errorf("resolve agents via %s returned no ranks", endpoint)
	}
	return result, nil
}

func (c *Client) Info(
	ctx context.Context,
	endpoint string,
	token string,
) (GatewayInfo, error) {
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		strings.TrimRight(endpoint, "/")+"/v1/info",
		nil,
	)
	if err != nil {
		return GatewayInfo{}, err
	}
	if token == "" {
		token = c.Token
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := c.HTTP.Do(request)
	if err != nil {
		return GatewayInfo{}, fmt.Errorf("read gateway info via %s: %w", endpoint, err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return GatewayInfo{}, fmt.Errorf("read gateway info response: %w", err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return GatewayInfo{}, fmt.Errorf(
			"read gateway info via %s: HTTP %d: %s",
			endpoint,
			response.StatusCode,
			strings.TrimSpace(string(body)),
		)
	}
	var result GatewayInfo
	if err := json.Unmarshal(body, &result); err != nil {
		return GatewayInfo{}, fmt.Errorf("decode gateway info: %w", err)
	}
	if result.Version == "" {
		return GatewayInfo{}, fmt.Errorf("gateway info is missing protocol version")
	}
	return result, nil
}

func (c *Client) Health(
	ctx context.Context,
	endpoint string,
	token string,
) (HealthResponse, error) {
	httpRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		strings.TrimRight(endpoint, "/")+"/v1/cluster",
		nil,
	)
	if err != nil {
		return HealthResponse{}, err
	}
	if token == "" {
		token = c.Token
	}
	httpRequest.Header.Set("Authorization", "Bearer "+token)
	response, err := c.HTTP.Do(httpRequest)
	if err != nil {
		return HealthResponse{}, fmt.Errorf("gateway health via %s: %w", endpoint, err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return HealthResponse{}, fmt.Errorf("read gateway health: %w", err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return HealthResponse{}, fmt.Errorf(
			"gateway health via %s: HTTP %d: %s",
			endpoint,
			response.StatusCode,
			strings.TrimSpace(string(body)),
		)
	}
	var result HealthResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return HealthResponse{}, fmt.Errorf("decode gateway health: %w", err)
	}
	if !result.Ready {
		return HealthResponse{}, fmt.Errorf("gateway %s is not ready", endpoint)
	}
	return result, nil
}
