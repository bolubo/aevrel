package artifactstore

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMemoryStorePublishesContentAddressedArtifact(t *testing.T) {
	store := NewMemoryStore("http://controller:8082")
	payload := []byte(`{"version":"1","nodes":[]}`)
	ref, err := store.Put(context.Background(), payload)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(ref.Hash, "sha256:") || ref.Size != int64(len(payload)) {
		t.Fatalf("unexpected ref: %+v", ref)
	}
	if ref.URL != "http://controller:8082/v1/artifacts/"+ref.Hash {
		t.Fatalf("unexpected URL: %s", ref.URL)
	}

	request := httptest.NewRequest(
		http.MethodGet,
		"/v1/artifacts/"+ref.Hash,
		nil,
	)
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d", response.Code)
	}
	if got := response.Body.String(); got != string(payload) {
		t.Fatalf("body = %q", got)
	}
	if response.Header().Get("ETag") != ref.Hash {
		t.Fatalf("ETag = %q", response.Header().Get("ETag"))
	}
}

func TestMemoryStoreRejectsMissingArtifact(t *testing.T) {
	store := NewMemoryStore("http://controller:8082")
	request := httptest.NewRequest(
		http.MethodGet,
		"/v1/artifacts/sha256:missing",
		nil,
	)
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", response.Code)
	}
}

func TestMemoryStoreRequiresConfiguredBearerToken(t *testing.T) {
	store := NewMemoryStore("http://controller:8082")
	store.SetToken("secret")
	ref, err := store.Put(context.Background(), []byte(`{"version":"1"}`))
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodGet, "/v1/artifacts/"+ref.Hash, nil)
	response := httptest.NewRecorder()
	store.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", response.Code)
	}

	request = httptest.NewRequest(http.MethodGet, "/v1/artifacts/"+ref.Hash, nil)
	request.Header.Set("Authorization", "Bearer secret")
	response = httptest.NewRecorder()
	store.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
}
