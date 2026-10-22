package kueueadapter

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	"github.com/shardferry/shardferry/internal/cutoverclient"
)

// HandoffTrigger is called after a protected workload is evicted. The
// implementation owns the actual pause -> final delta -> promote transaction.
type HandoffTrigger interface {
	Trigger(ctx context.Context) (string, error)
}

type CutoverHandoff struct {
	Client *cutoverclient.Client
}

func (h CutoverHandoff) Trigger(ctx context.Context) (string, error) {
	if h.Client == nil {
		return "", fmt.Errorf("cutover client is not configured")
	}
	result, err := h.Client.Cutover(ctx, "")
	if err != nil {
		return "", err
	}
	state, _ := result["state"].(string)
	if state == "" {
		state = "requested"
	}
	return state, nil
}

func isEvicted(workload *unstructured.Unstructured) bool {
	conditions, found, _ := unstructured.NestedSlice(
		workload.Object,
		"status",
		"conditions",
	)
	if !found {
		return false
	}
	for _, raw := range conditions {
		condition, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		kind, _, _ := unstructured.NestedString(condition, "type")
		status, _, _ := unstructured.NestedString(condition, "status")
		if kind == "Evicted" && status == "True" {
			return true
		}
	}
	return false
}
