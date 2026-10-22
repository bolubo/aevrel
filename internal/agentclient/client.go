package agentclient

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Tensor struct {
	Name  string  `json:"name"`
	Shape []int32 `json:"shape"`
	Dtype string  `json:"dtype"`
}

type Chunk struct {
	ID           string    `json:"id,omitempty"`
	Tensor       string    `json:"tensor"`
	Rank         int32     `json:"rank"`
	File         string    `json:"file"`
	TensorOffset []int32   `json:"tensor_offset"`
	TensorSize   []int32   `json:"tensor_size"`
	ByteRanges   [][]int64 `json:"byte_ranges"`
	Checksum     string    `json:"checksum"`
}

type Inventory struct {
	Version      string   `json:"version"`
	Rank         int32    `json:"rank"`
	Format       string   `json:"format"`
	MetadataHash string   `json:"metadata_hash"`
	Tensors      []Tensor `json:"tensors"`
	Chunks       []Chunk  `json:"chunks"`
	AgentVersion string   `json:"agent_version"`
	TorchVersion string   `json:"torch_version"`
}

type RanksResponse struct {
	Version string  `json:"version"`
	Ranks   []int32 `json:"ranks"`
}

type PlanResponse struct {
	Version string         `json:"version"`
	Plan    map[string]any `json:"plan"`
}

type ProofResponse struct {
	Version string         `json:"version"`
	Proof   map[string]any `json:"proof"`
}

type Task struct {
	TaskID       string         `json:"task_id,omitempty"`
	Version      string         `json:"version"`
	MigrationUID string         `json:"migration_uid"`
	CommitToken  string         `json:"commit_token,omitempty"`
	Rank         int32          `json:"rank"`
	TaskHash     string         `json:"task_hash"`
	TargetRoot   string         `json:"target_root"`
	SpecRef      string         `json:"spec_ref"`
	PlanRef      string         `json:"plan_ref"`
	PlanSlice    map[string]any `json:"plan_slice"`
	Report       map[string]any `json:"report,omitempty"`
	State        string         `json:"state"`
}

type AgentError struct {
	StatusCode int
	Code       string
	Category   string
	Phase      string
	Rank       int32
	Details    string
	Retryable  *bool
}

func (e *AgentError) Error() string {
	return fmt.Sprintf("agent error %d (%s): %s", e.StatusCode, e.Code, e.Details)
}

func (e *AgentError) IsRetryable() bool {
	if e.Retryable != nil {
		return *e.Retryable
	}
	switch e.StatusCode {
	case http.StatusRequestTimeout, http.StatusTooEarly, http.StatusTooManyRequests:
		return true
	case http.StatusConflict:
		return true
	}
	return e.StatusCode >= http.StatusInternalServerError
}

func Retryable(err error) bool {
	var agentErr *AgentError
	if errors.As(err, &agentErr) {
		return agentErr.IsRetryable()
	}
	return errors.Is(err, context.DeadlineExceeded)
}

type Client struct {
	HTTP           *http.Client
	Token          string
	mu             sync.RWMutex
	endpointTokens map[string]string
}

func New(token string) *Client {
	return &Client{
		HTTP:           &http.Client{Timeout: 30 * time.Second},
		Token:          token,
		endpointTokens: make(map[string]string),
	}
}

func (c *Client) RegisterEndpointToken(endpoint, token string) {
	endpoint = strings.TrimRight(endpoint, "/")
	if endpoint == "" || token == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.endpointTokens == nil {
		c.endpointTokens = make(map[string]string)
	}
	c.endpointTokens[endpoint] = token
}

func (c *Client) tokenFor(endpoint string) string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	best := ""
	token := c.Token
	for base, candidate := range c.endpointTokens {
		if endpoint == base || strings.HasPrefix(endpoint, base+"/") {
			if len(base) > len(best) {
				best = base
				token = candidate
			}
		}
	}
	return token
}

func (c *Client) Inventory(
	ctx context.Context,
	endpoint string,
	rank int32,
) (Inventory, error) {
	var inventory Inventory
	path := "/v1/inventory?rank=" + strconv.Itoa(int(rank))
	if err := c.do(ctx, http.MethodGet, endpoint+path, nil, &inventory); err != nil {
		return Inventory{}, err
	}
	return inventory, nil
}

func (c *Client) Metadata(
	ctx context.Context,
	endpoint string,
) ([]byte, string, error) {
	data, headers, err := c.doBytes(
		ctx,
		http.MethodGet,
		endpoint+"/v1/metadata",
		nil,
		"",
	)
	if err != nil {
		return nil, "", err
	}
	sum := sha256.Sum256(data)
	digest := "sha256:" + hex.EncodeToString(sum[:])
	if header := headers.Get("X-Metadata-SHA256"); header != "" && header != digest {
		return nil, "", fmt.Errorf(
			"metadata digest mismatch: header %s, body %s",
			header,
			digest,
		)
	}
	return data, digest, nil
}

func (c *Client) PutMetadata(
	ctx context.Context,
	endpoint string,
	data []byte,
) (string, error) {
	response, _, err := c.doBytes(
		ctx,
		http.MethodPost,
		endpoint+"/v1/metadata",
		data,
		"application/octet-stream",
	)
	if err != nil {
		return "", err
	}
	var payload struct {
		MetadataHash string `json:"metadata_hash"`
	}
	if err := json.Unmarshal(response, &payload); err != nil {
		return "", fmt.Errorf("decode metadata response: %w", err)
	}
	if payload.MetadataHash == "" {
		return "", fmt.Errorf("metadata response has no hash")
	}
	return payload.MetadataHash, nil
}

func (c *Client) Ranks(
	ctx context.Context,
	endpoint string,
) ([]int32, error) {
	var response RanksResponse
	if err := c.do(
		ctx,
		http.MethodGet,
		endpoint+"/v1/ranks",
		nil,
		&response,
	); err != nil {
		return nil, err
	}
	return response.Ranks, nil
}

func (c *Client) Capabilities(
	ctx context.Context,
	endpoint string,
) (map[string]any, error) {
	var response map[string]any
	if err := c.do(ctx, http.MethodGet, endpoint+"/v1/capabilities", nil, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) Preflight(
	ctx context.Context,
	endpoint string,
	targetRoot string,
	requiredBytes int64,
) (map[string]any, error) {
	var response map[string]any
	if err := c.do(
		ctx,
		http.MethodPost,
		endpoint+"/v1/preflight",
		map[string]any{
			"target_root":    targetRoot,
			"required_bytes": requiredBytes,
		},
		&response,
	); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) CleanupTarget(
	ctx context.Context,
	endpoint string,
	targetRoot string,
	purgeTarget bool,
) (map[string]any, error) {
	var response map[string]any
	if err := c.do(
		ctx,
		http.MethodPost,
		endpoint+"/v1/cleanup",
		map[string]any{
			"target_root":  targetRoot,
			"purge_target": purgeTarget,
		},
		&response,
	); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) Plan(
	ctx context.Context,
	endpoint string,
	spec map[string]any,
) (PlanResponse, error) {
	var response PlanResponse
	body := map[string]any{"version": "1", "spec": spec}
	if err := c.do(ctx, http.MethodPost, endpoint+"/v1/plan", body, &response); err != nil {
		return PlanResponse{}, err
	}
	return response, nil
}

func (c *Client) MergeProof(
	ctx context.Context,
	endpoint string,
	proofs []map[string]any,
) (map[string]any, error) {
	var response ProofResponse
	body := map[string]any{"proofs": proofs}
	if err := c.do(ctx, http.MethodPost, endpoint+"/v1/proof/merge", body, &response); err != nil {
		return nil, err
	}
	if response.Proof == nil {
		return nil, fmt.Errorf("proof merge returned an empty proof")
	}
	return response.Proof, nil
}

func (c *Client) CreateTask(
	ctx context.Context,
	endpoint string,
	task Task,
) (Task, error) {
	var response Task
	if err := c.do(ctx, http.MethodPost, endpoint+"/v1/tasks", task, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) PrepareTask(ctx context.Context, endpoint, taskID string) (Task, error) {
	return c.taskAction(ctx, endpoint, taskID, "prepare")
}

func (c *Client) ExecuteTask(ctx context.Context, endpoint, taskID string) (Task, error) {
	return c.taskAction(ctx, endpoint, taskID, "execute")
}

func (c *Client) CommitTask(ctx context.Context, endpoint, taskID string) (Task, error) {
	return c.taskAction(ctx, endpoint, taskID, "commit")
}

func (c *Client) CancelTask(ctx context.Context, endpoint, taskID string) (Task, error) {
	return c.taskAction(ctx, endpoint, taskID, "cancel")
}

func (c *Client) GetTask(ctx context.Context, endpoint, taskID string) (Task, error) {
	var task Task
	path := endpoint + "/v1/tasks/" + url.PathEscape(taskID)
	if err := c.do(ctx, http.MethodGet, path, nil, &task); err != nil {
		return Task{}, err
	}
	return task, nil
}

func (c *Client) taskAction(
	ctx context.Context,
	endpoint string,
	taskID string,
	action string,
) (Task, error) {
	var task Task
	path := endpoint + "/v1/tasks/" + url.PathEscape(taskID) + "/" + action
	if err := c.do(ctx, http.MethodPost, path, map[string]any{}, &task); err != nil {
		return Task{}, err
	}
	return task, nil
}

func (c *Client) do(
	ctx context.Context,
	method string,
	endpoint string,
	body any,
	out any,
) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode request: %w", err)
		}
		reader = bytes.NewReader(data)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+c.tokenFor(endpoint))
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return agentErrorFromResponse(response.StatusCode, data)
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}
	return nil
}

func (c *Client) doBytes(
	ctx context.Context,
	method string,
	endpoint string,
	body []byte,
	contentType string,
) ([]byte, http.Header, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return nil, nil, err
	}
	request.Header.Set("Authorization", "Bearer "+c.tokenFor(endpoint))
	if contentType != "" {
		request.Header.Set("Content-Type", contentType)
	}
	response, err := c.HTTP.Do(request)
	if err != nil {
		return nil, nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, nil, agentErrorFromResponse(response.StatusCode, data)
	}
	return data, response.Header, nil
}

func agentErrorFromResponse(status int, data []byte) error {
	var envelope struct {
		Code      string `json:"code"`
		Category  string `json:"category"`
		Phase     string `json:"phase"`
		Rank      int32  `json:"rank"`
		Details   string `json:"details"`
		Retryable *bool  `json:"retryable"`
	}
	if json.Unmarshal(data, &envelope) == nil && envelope.Code != "" {
		return &AgentError{
			StatusCode: status,
			Code:       envelope.Code,
			Category:   envelope.Category,
			Phase:      envelope.Phase,
			Rank:       envelope.Rank,
			Details:    envelope.Details,
			Retryable:  envelope.Retryable,
		}
	}
	return &AgentError{
		StatusCode: status,
		Code:       fmt.Sprintf("HTTP_%d", status),
		Details:    strings.TrimSpace(string(data)),
	}
}
