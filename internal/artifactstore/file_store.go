package artifactstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

type FileStore struct {
	mu      sync.RWMutex
	baseURL string
	root    string
	token   string
}

func NewFileStore(baseURL, root string) (*FileStore, error) {
	absolute, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(absolute, 0o750); err != nil {
		return nil, err
	}
	return &FileStore{
		baseURL: strings.TrimRight(baseURL, "/"),
		root:    absolute,
	}, nil
}

func (s *FileStore) SetToken(token string) {
	s.mu.Lock()
	s.token = token
	s.mu.Unlock()
}

func (s *FileStore) Put(_ context.Context, payload []byte) (Ref, error) {
	sum := sha256.Sum256(payload)
	hash := "sha256:" + hex.EncodeToString(sum[:])
	path := filepath.Join(s.root, strings.TrimPrefix(hash, "sha256:"))
	ref := Ref{
		URL:  s.baseURL + "/v1/artifacts/" + hash,
		Hash: hash,
		Size: int64(len(payload)),
	}
	if _, err := os.Stat(path); err == nil {
		return ref, nil
	}
	temporary, err := os.CreateTemp(s.root, ".artifact-*")
	if err != nil {
		return Ref{}, err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if _, err := temporary.Write(payload); err != nil {
		temporary.Close()
		return Ref{}, err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return Ref{}, err
	}
	if err := temporary.Close(); err != nil {
		return Ref{}, err
	}
	if err := os.Rename(temporaryName, path); err != nil {
		return Ref{}, err
	}
	return ref, nil
}

func (s *FileStore) Get(_ context.Context, ref string) ([]byte, error) {
	name := artifactName(ref)
	if len(name) != 64 {
		return nil, fmt.Errorf("invalid artifact hash: %s", ref)
	}
	if _, err := hex.DecodeString(name); err != nil {
		return nil, fmt.Errorf("invalid artifact hash: %s", ref)
	}
	payload, err := os.ReadFile(filepath.Join(s.root, name))
	if err != nil {
		return nil, fmt.Errorf("artifact not found: %s", ref)
	}
	return payload, nil
}

func (s *FileStore) Delete(_ context.Context, ref string) error {
	name := artifactName(ref)
	if len(name) != 64 {
		return fmt.Errorf("invalid artifact hash: %s", ref)
	}
	if _, err := hex.DecodeString(name); err != nil {
		return fmt.Errorf("invalid artifact hash: %s", ref)
	}
	if err := os.Remove(filepath.Join(s.root, name)); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

func (s *FileStore) Handler() http.Handler {
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
		ref := strings.TrimPrefix(r.URL.Path, prefix)
		payload, err := s.Get(r.Context(), ref)
		if err != nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ETag", ref)
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(payload)
	})
}

func artifactName(ref string) string {
	if index := strings.LastIndex(ref, "/v1/artifacts/"); index >= 0 {
		ref = ref[index+len("/v1/artifacts/"):]
	}
	return strings.TrimPrefix(ref, "sha256:")
}
