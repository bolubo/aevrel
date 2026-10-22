package targetready

import (
	"encoding/json"
	"fmt"
	"time"
)

// Aggregate is the immutable proof published after a target validation run.
type Aggregate struct {
	SchemaVersion        string       `json:"schemaVersion"`
	MigrationUID         string       `json:"migrationUID"`
	Version              string       `json:"version"`
	Ready                bool         `json:"ready"`
	Condition            string       `json:"condition"`
	Message              string       `json:"message"`
	RanksReady           int32        `json:"ranksReady"`
	RanksTotal           int32        `json:"ranksTotal"`
	StepsRan             int32        `json:"stepsRan"`
	StateComplete        bool         `json:"stateComplete"`
	DurationMilliseconds int64        `json:"durationMilliseconds,omitempty"`
	JobName              string       `json:"jobName,omitempty"`
	StartedAt            string       `json:"startedAt,omitempty"`
	CompletedAt          string       `json:"completedAt"`
	MissingRanks         []int32      `json:"missingRanks,omitempty"`
	Reports              []RankReport `json:"reports"`
}

func NewAggregate(
	migrationUID string,
	version string,
	jobName string,
	startedAt string,
	evaluation Evaluation,
) Aggregate {
	return Aggregate{
		SchemaVersion: SchemaVersion,
		MigrationUID:  migrationUID,
		Version:       version,
		Ready:         evaluation.Ready,
		Condition:     evaluation.Condition,
		Message:       evaluation.Message,
		RanksReady:    evaluation.RanksReady,
		RanksTotal:    evaluation.RanksTotal,
		StepsRan:      evaluation.StepsRan,
		StateComplete: evaluation.StateComplete,
		JobName:       jobName,
		StartedAt:     startedAt,
		CompletedAt:   time.Now().UTC().Format(time.RFC3339),
		MissingRanks:  append([]int32(nil), evaluation.MissingRanks...),
		Reports:       append([]RankReport{}, evaluation.Reports...),
	}
}

func (a Aggregate) Marshal() ([]byte, error) {
	payload, err := json.MarshalIndent(a, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("encode target-ready aggregate: %w", err)
	}
	return append(payload, '\n'), nil
}
