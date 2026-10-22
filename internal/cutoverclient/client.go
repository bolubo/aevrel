package cutoverclient

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
)

// Client talks to the Python CutoverService control plane.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
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

func (c *Client) Status(ctx context.Context) (map[string]any, error) {
	var response map[string]any
	if err := c.do(ctx, http.MethodGet, "/v1/status", nil, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) Follow(ctx context.Context) ([]map[string]any, error) {
	var response struct {
		Results []map[string]any `json:"results"`
	}
	if err := c.do(ctx, http.MethodPost, "/v1/follow", map[string]any{}, &response); err != nil {
		return nil, err
	}
	return response.Results, nil
}

func (c *Client) Cutover(
	ctx context.Context,
	versionID string,
) (map[string]any, error) {
	var response map[string]any
	body := map[string]any{}
	if versionID != "" {
		body["version_id"] = versionID
	}
	if err := c.do(ctx, http.MethodPost, "/v1/cutover", body, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) Recover(ctx context.Context) (map[string]any, error) {
	var response map[string]any
	if err := c.do(ctx, http.MethodPost, "/v1/recover", map[string]any{}, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) do(
	ctx context.Context,
	method string,
	path string,
	body any,
	out any,
) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode cutover request: %w", err)
		}
		reader = bytes.NewReader(data)
	}
	request, err := http.NewRequestWithContext(
		ctx,
		method,
		c.BaseURL+path,
		reader,
	)
	if err != nil {
		return err
	}
	if c.Token != "" {
		request.Header.Set("Authorization", "Bearer "+c.Token)
	}
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
		return fmt.Errorf(
			"cutover service HTTP %d: %s",
			response.StatusCode,
			strings.TrimSpace(string(data)),
		)
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			return fmt.Errorf("decode cutover response: %w", err)
		}
	}
	return nil
}

func loadTLSConfig(caFile, certFile, keyFile string) (*tls.Config, error) {
	if certFile == "" || keyFile == "" {
		return nil, fmt.Errorf("TLS cert and key are required together")
	}
	certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load cutover client certificate: %w", err)
	}
	config := &tls.Config{
		MinVersion:   tls.VersionTLS12,
		Certificates: []tls.Certificate{certificate},
	}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil {
			return nil, fmt.Errorf("read cutover CA: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(data) {
			return nil, fmt.Errorf("cutover CA contains no certificates")
		}
		config.RootCAs = pool
	}
	return config, nil
}
