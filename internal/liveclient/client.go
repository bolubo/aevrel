package liveclient

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/controller"
)

const reconcilePath = "/v1/live/reconcile"

// Client drives the Python LRS data plane through an idempotent reconcile
// contract. The controller owns CR status; the service only reports observed
// ownership/coverage state.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

type migrationRef struct {
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	UID        string `json:"uid,omitempty"`
	Generation int64  `json:"generation,omitempty"`
}

type reconcileRequest struct {
	APIVersion     string                           `json:"apiVersion"`
	Kind           string                           `json:"kind"`
	Migration      migrationRef                     `json:"migration"`
	Spec           aevrelv1.CheckpointMigrationSpec `json:"spec"`
	PreviousStatus *aevrelv1.LiveRankSplitStatus    `json:"previousStatus,omitempty"`
}

func New(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP:    &http.Client{Timeout: 30 * time.Second},
	}
}

func NewWithTLS(
	baseURL, token, caFile, certFile, keyFile string,
) (*Client, error) {
	tlsConfig, err := loadTLSConfig(caFile, certFile, keyFile)
	if err != nil {
		return nil, err
	}
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP: &http.Client{
			Timeout: 30 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: tlsConfig,
			},
		},
	}, nil
}

func (c *Client) Observe(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (controller.LiveRankSplitObservation, error) {
	if migration.Spec.Live == nil {
		return controller.LiveRankSplitObservation{}, fmt.Errorf(
			"live-rank-split spec is missing",
		)
	}
	baseURL := c.BaseURL
	if migration.Spec.Live.HandoffEndpoint != "" {
		baseURL = strings.TrimRight(migration.Spec.Live.HandoffEndpoint, "/")
	}
	if baseURL == "" {
		return controller.LiveRankSplitObservation{}, fmt.Errorf(
			"live.handoffEndpoint is required",
		)
	}
	body := reconcileRequest{
		APIVersion: "aevrel.dev/v1",
		Kind:       "LiveRankSplitReconcile",
		Migration: migrationRef{
			Namespace:  migration.Namespace,
			Name:       migration.Name,
			UID:        string(migration.UID),
			Generation: migration.Generation,
		},
		Spec:           migration.Spec,
		PreviousStatus: migration.Status.Live.DeepCopy(),
	}
	var observation controller.LiveRankSplitObservation
	if err := c.do(ctx, baseURL, body, &observation); err != nil {
		return controller.LiveRankSplitObservation{}, err
	}
	return observation, nil
}

func (c *Client) do(
	ctx context.Context,
	baseURL string,
	body reconcileRequest,
	out *controller.LiveRankSplitObservation,
) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("encode live-rank-split request: %w", err)
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		baseURL+reconcilePath,
		bytes.NewReader(data),
	)
	if err != nil {
		return err
	}
	if c.Token != "" {
		request.Header.Set("Authorization", "Bearer "+c.Token)
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := c.HTTP.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf(
			"live service HTTP %d: %s",
			response.StatusCode,
			strings.TrimSpace(string(payload)),
		)
	}
	if err := json.Unmarshal(payload, out); err != nil {
		return fmt.Errorf("decode live-rank-split response: %w", err)
	}
	return nil
}

func loadTLSConfig(caFile, certFile, keyFile string) (*tls.Config, error) {
	if certFile == "" || keyFile == "" {
		return nil, fmt.Errorf("TLS cert and key are required together")
	}
	certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load live client certificate: %w", err)
	}
	config := &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{certificate},
	}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("read live CA: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(data) {
			return nil, fmt.Errorf("live CA contains no certificates")
		}
		config.RootCAs = pool
	}
	return config, nil
}
