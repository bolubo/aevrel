package kueueadapter

import (
	"fmt"
	"math"
)

// Reason is a stable, user-visible explanation for a protection decision.
type Reason string

const (
	ReasonInvalidStateProtected Reason = "InvalidStateProtected"
	ReasonUnknownStateProtected Reason = "UnknownStateProtected"
	ReasonCheckpointIncomplete  Reason = "CheckpointIncomplete"
	ReasonProgressThreshold     Reason = "ProgressThreshold"
	ReasonInvalidStateEligible  Reason = "InvalidStateEligible"
	ReasonUnknownStateEligible  Reason = "UnknownStateEligible"
	ReasonEligible              Reason = "Eligible"
)

// Observation is the checkpoint state published by a training workload.
//
// Invalid means the publisher supplied checkpoint fields that could not be
// parsed. It is kept separate from an absent observation so the adapter can
// explain why an unknown workload was protected.
type Observation struct {
	Known                   bool
	Invalid                 bool
	TrainingProgressPercent int
	CheckpointComplete      bool
	CheckpointVersion       string
	CheckpointAgeSeconds    *int64
}

// Policy is deliberately binary. It does not score workloads or reorder
// Kueue candidates.
type Policy struct {
	ProtectProgressGTE        float64
	RequireCheckpointComplete bool
	ProtectUnknown            bool
}

// Decision is the only output consumed by the Kubernetes adapter.
type Decision struct {
	Protected bool
	Eligible  bool
	Reason    Reason
}

// DefaultPolicy protects a workload after it reaches 80% progress or whenever
// checkpoint state is incomplete or unknown.
func DefaultPolicy() Policy {
	return Policy{
		ProtectProgressGTE:        0.8,
		RequireCheckpointComplete: true,
		ProtectUnknown:            true,
	}
}

func (p Policy) Validate() error {
	if math.IsNaN(p.ProtectProgressGTE) || math.IsInf(p.ProtectProgressGTE, 0) {
		return fmt.Errorf("protect-progress-gte must be finite")
	}
	if p.ProtectProgressGTE < 0 || p.ProtectProgressGTE > 1 {
		return fmt.Errorf("protect-progress-gte must be between 0 and 1")
	}
	return nil
}

func (p Policy) Decide(observation Observation) Decision {
	if !observation.Known || observation.Invalid {
		if p.ProtectUnknown {
			reason := ReasonUnknownStateProtected
			if observation.Invalid {
				reason = ReasonInvalidStateProtected
			}
			return protected(reason)
		}
		reason := ReasonUnknownStateEligible
		if observation.Invalid {
			reason = ReasonInvalidStateEligible
		}
		return eligible(reason)
	}
	if p.RequireCheckpointComplete && !observation.CheckpointComplete {
		return protected(ReasonCheckpointIncomplete)
	}
	progress := float64(observation.TrainingProgressPercent) / 100
	if progress >= p.ProtectProgressGTE {
		return protected(ReasonProgressThreshold)
	}
	return eligible(ReasonEligible)
}

func protected(reason Reason) Decision {
	return Decision{
		Protected: true,
		Reason:    reason,
	}
}

func eligible(reason Reason) Decision {
	return Decision{
		Eligible: true,
		Reason:   reason,
	}
}
