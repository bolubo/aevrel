package artifactstore

import (
	"context"
	"testing"
)

func TestFileStoreDeleteRemovesArtifact(t *testing.T) {
	store, err := NewFileStore("http://controller:8082", t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	ref, err := store.Put(context.Background(), []byte(`{"ok":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Get(context.Background(), ref.URL); err != nil {
		t.Fatal(err)
	}
	if err := store.Delete(context.Background(), ref.URL); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Get(context.Background(), ref.URL); err == nil {
		t.Fatal("artifact still exists after delete")
	}
	if err := store.Delete(context.Background(), ref.URL); err != nil {
		t.Fatalf("idempotent delete failed: %v", err)
	}
}
