package execution

import (
	"context"
	"errors"

	"github.com/shardferry/shardferry/internal/agentclient"
)

func isRetryable(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) {
		return false
	}
	var agentErr *agentclient.AgentError
	if errors.As(err, &agentErr) {
		return agentErr.IsRetryable()
	}
	return true
}
