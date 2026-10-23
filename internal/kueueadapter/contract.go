package kueueadapter

import (
	"fmt"
	"strconv"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	// AnnotationProtection is the explicit opt-in. The adapter does not modify
	// unrelated Kueue Workloads.
	AnnotationProtection       = "shardferry.io/preemption-protection"
	AnnotationCheckpointRecord = "shardferry.io/checkpoint-record"

	// Checkpoint publisher contract. Annotations are authoritative; labels are
	// accepted as a fallback for simple sidecars.
	AnnotationProgressPercent    = "shardferry.io/training-progress-percent"
	AnnotationCheckpointComplete = "shardferry.io/checkpoint-complete"
	AnnotationCheckpointVersion  = "shardferry.io/checkpoint-version"
	AnnotationCheckpointAge      = "shardferry.io/checkpoint-age-seconds"

	// Labels consumed by Kueue PreemptionConfig.numericLabels.
	LabelPreemptionProtected = "shardferry.io/preemption-protected"
	LabelPreemptionReason    = "shardferry.io/preemption-reason"
	LabelProgressPercent     = "shardferry.io/training-progress-percent"
	LabelCheckpointComplete  = "shardferry.io/checkpoint-complete"
	LabelDecisionSource      = "shardferry.io/preemption-decision-source"
)

func IsProtectionEnabled(object metav1.Object) bool {
	for _, source := range []map[string]string{
		object.GetAnnotations(),
		object.GetLabels(),
	} {
		value := strings.TrimSpace(strings.ToLower(source[AnnotationProtection]))
		if value == "enabled" || value == "true" || value == "1" {
			return true
		}
	}
	return false
}

// ParseObservation reads checkpoint state from a Job or Workload. Missing
// fields are not invented. If at least one supported field is present, the
// observation is known; malformed fields make it invalid and therefore
// protectable under the unknown-state policy.
func ParseObservation(
	annotations map[string]string,
	labels map[string]string,
) (Observation, error) {
	var observation Observation
	var present bool
	var errs []string

	progress, ok := lookup(annotations, labels, AnnotationProgressPercent)
	if ok {
		present = true
		value, err := strconv.Atoi(strings.TrimSpace(progress))
		if err != nil || value < 0 || value > 100 {
			errs = append(errs, fmt.Sprintf(
				"%s must be an integer between 0 and 100",
				AnnotationProgressPercent,
			))
		} else {
			observation.TrainingProgressPercent = value
		}
	}

	complete, ok := lookup(annotations, labels, AnnotationCheckpointComplete)
	if ok {
		present = true
		value, err := parseBool(complete)
		if err != nil {
			errs = append(errs, fmt.Sprintf(
				"%s must be one of 1, 0, true, false",
				AnnotationCheckpointComplete,
			))
		} else {
			observation.CheckpointComplete = value
		}
	}

	version, ok := lookup(annotations, labels, AnnotationCheckpointVersion)
	if ok {
		present = true
		observation.CheckpointVersion = strings.TrimSpace(version)
	}

	age, ok := lookup(annotations, labels, AnnotationCheckpointAge)
	if ok {
		present = true
		value, err := strconv.ParseInt(strings.TrimSpace(age), 10, 64)
		if err != nil || value < 0 {
			errs = append(errs, fmt.Sprintf(
				"%s must be a non-negative integer",
				AnnotationCheckpointAge,
			))
		} else {
			observation.CheckpointAgeSeconds = &value
		}
	}

	observation.Known = present
	if len(errs) > 0 {
		observation.Known = false
		observation.Invalid = true
		return observation, fmt.Errorf("%s", strings.Join(errs, "; "))
	}
	return observation, nil
}

func lookup(
	annotations map[string]string,
	labels map[string]string,
	key string,
) (string, bool) {
	if value, ok := annotations[key]; ok {
		return value, true
	}
	value, ok := labels[key]
	return value, ok
}

func parseBool(value string) (bool, error) {
	switch strings.TrimSpace(strings.ToLower(value)) {
	case "1", "true":
		return true, nil
	case "0", "false":
		return false, nil
	default:
		return false, fmt.Errorf("invalid boolean %q", value)
	}
}
