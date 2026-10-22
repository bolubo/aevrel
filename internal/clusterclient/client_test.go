package clusterclient

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestInfoReadsGatewayVersion(t *testing.T) {
	client := New("")
	client.HTTP.Transport = roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Path != "/v1/info" {
			t.Fatalf("path = %q", request.URL.Path)
		}
		if request.Header.Get("Authorization") != "Bearer token-1" {
			t.Fatalf("authorization = %q", request.Header.Get("Authorization"))
		}
		body, err := json.Marshal(GatewayInfo{
			Version:        "1",
			Cluster:        "source",
			GatewayVersion: "0.2.0",
		})
		if err != nil {
			t.Fatal(err)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       io.NopCloser(strings.NewReader(string(body))),
			Header:     make(http.Header),
		}, nil
	})
	info, err := client.Info(context.Background(), "http://gateway.example", "token-1")
	if err != nil {
		t.Fatal(err)
	}
	if info.Version != "1" || info.Cluster != "source" ||
		info.GatewayVersion != "0.2.0" {
		t.Fatalf("info = %#v", info)
	}
}
