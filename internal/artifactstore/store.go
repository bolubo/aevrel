package artifactstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"sync"
)

type Ref struct {
	URL  string
	Hash string
	Size int64
}

type Store interface {
	Put(ctx context.Context, payload []byte) (Ref, error)
	Get(ctx context.Context, ref string) ([]byte, error)
}

type Deleter interface {
	Delete(ctx context.Context, ref string) error
}

type MemoryStore struct {
	mu      sync.RWMutex
	baseURL string
	token   string
	items   map[string][]byte
}

func (s *MemoryStore) SetToken(token string) {
	s.mu.Lock()
	s.token = token
	s.mu.Unlock()
}

func NewMemoryStore(baseURL string) *MemoryStore {
	return &MemoryStore{
		baseURL: strings.TrimRight(baseURL, "/"),
		items:   make(map[string][]byte),
	}
}

func (s *MemoryStore) Put(_ context.Context, payload []byte) (Ref, error) {
	sum := sha256.Sum256(payload)
	hash := "sha256:" + hex.EncodeToString(sum[:])
	s.mu.Lock()
	s.items[hash] = append([]byte(nil), payload...)
	s.mu.Unlock()
	return Ref{
		URL:  s.baseURL + "/v1/artifacts/" + hash,
		Hash: hash,
		Size: int64(len(payload)),
	}, nil
}

func (s *MemoryStore) Get(_ context.Context, ref string) ([]byte, error) {
	hash := ref
	if index := strings.LastIndex(hash, "/v1/artifacts/"); index >= 0 {
		hash = hash[index+len("/v1/artifacts/"):]
	}
	s.mu.RLock()
	payload, ok := s.items[hash]
	s.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("artifact not found: %s", hash)
	}
	return append([]byte(nil), payload...), nil
}

func (s *MemoryStore) Delete(_ context.Context, ref string) error {
	hash := artifactName(ref)
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.items, hash)
	return nil
}

func (s *MemoryStore) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		s.mu.RLock()
		token := s.token
		s.mu.RUnlock()
		if token != "" && r.Header.Get("Authorization") != "Bearer "+token {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		const prefix = "/v1/artifacts/"
		if !strings.HasPrefix(r.URL.Path, prefix) {
			http.NotFound(w, r)
			return
		}
		hash := strings.TrimPrefix(r.URL.Path, prefix)
		if hash == "" || strings.Contains(hash, "/") {
			http.NotFound(w, r)
			return
		}
		payload, err := s.Get(r.Context(), hash)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ETag", hash)
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(payload)
	})
}
