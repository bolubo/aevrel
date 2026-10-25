package kueueadapter

import (
	"time"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

// SchemaVersion is the single version every scheduler fact shares. A consumer
// confirms it once and every fact below is interpretable; adding a fact never
// invalidates the facts that already exist.
const SchemaVersion = "aevrel.dev/v1"

// SchemaLabelValue is the label-safe form of SchemaVersion. Kubernetes label
// values cannot contain "/", so the version travels as "v1" next to the
// aevrel.dev key prefix that already carries the domain.
const SchemaLabelValue = "v1"

// Fact sources. A fact set is only ever as trustworthy as the thing it came
// from, so the source travels with the facts instead of being inferred.
const (
	FactSourceCheckpointRecord          = "checkpoint-record"
	FactSourceCheckpointRecordAmbiguous = "checkpoint-record-ambiguous"
	FactSourceAnnotationFallback        = "annotation-fallback"
	FactSourceUnknown                   = "unknown"
)

// Envelope reason codes describe the envelope itself. They are deliberately
// separate from CheckpointFacts.ReasonCodes, which carries the readiness
// reasons unchanged so a consumer can map them 1:1 onto the readiness layer.
const (
	FactReasonNoCheckpointRecord = "NoCheckpointRecord"
	FactReasonAmbiguousRecords   = "AmbiguousCheckpointRecords"
	FactReasonAnnotationFallback = "AnnotationFallback"
	FactReasonAnnotationInvalid  = "AnnotationInvalid"
	FactReasonStaleObservation   = "StaleObservation"
)

// rescueEventAdvisory is metadata only. The envelope never claims that a
// preemption happened; DecideRescue echoes the event and derives its action
// from the readiness alone.
const rescueEventAdvisory = "Advisory"

// CheckpointFacts is the read-only projection of one checkpoint's readiness.
//
// Every field mirrors CheckpointRecord.Status.Readiness. Nothing here is
// scored, defaulted, or reinterpreted, and pointer fields use nil for
// "unknown": estimatedRTOSeconds is never 0, and complete/fresh are never
// guessed, because an absent verdict must stay absent.
//
// Facts are transport, not truth. CheckpointRecord.Status.Readiness stays the
// only authoritative source; this struct can always be recomputed from it.
type CheckpointFacts struct {
	SchemaVersion   string   `json:"schemaVersion"`
	Status          string   `json:"status"`
	Complete        *bool    `json:"complete"`
	Fresh           *bool    `json:"fresh"`
	Loadable        *bool    `json:"loadable"`
	Movable         *bool    `json:"movable"`
	ObservedAt      string   `json:"observedAt"`
	Source          string   `json:"source"`
	ReasonCodes     []string `json:"reasonCodes"`
	EnvelopeReasons []string `json:"envelopeReasons,omitempty"`

	// RestoreMode is the advisory action from control.DecideRescue. It is a
	// recommendation, not a command, and it never replaces the scheduler's own
	// decision.
	RestoreMode       string `json:"restoreMode"`
	RestoreReasonCode string `json:"restoreReasonCode,omitempty"`
	RestoreReason     string `json:"restoreReason,omitempty"`

	EstimatedRTOSeconds *int64 `json:"estimatedRTOSeconds"`

	SelectedRecord string `json:"selectedRecord,omitempty"`
	Candidates     int    `json:"candidates"`
	Ambiguous      bool   `json:"ambiguous"`

	TargetWorldSize  *int32 `json:"targetWorldSize,omitempty"`
	LastDrill        string `json:"lastDrill,omitempty"`
	LastDrillVersion string `json:"lastDrillVersion,omitempty"`
	LastDrillDigest  string `json:"lastDrillDigest,omitempty"`
	StateComplete    *bool  `json:"stateComplete,omitempty"`
	ReadinessLevel   string `json:"readinessLevel,omitempty"`

	// Conformance is the verdict of projecting this fact set. A fact set that
	// fails it is never published as labels; the binary protection decision is
	// unaffected.
	Conformance ConformanceReport `json:"conformance"`
}

// FactsOptions controls the only two things the envelope is allowed to depend
// on: the clock, and how old an observation may be before freshness is
// withdrawn. The zero value means "wall clock, no age limit", so no magic
// constant decides staleness on anyone's behalf.
type FactsOptions struct {
	Now               time.Time
	MaxObservationAge time.Duration
}

func (o FactsOptions) clock() time.Time {
	if o.Now.IsZero() {
		return time.Now()
	}
	return o.Now.UTC()
}

// CheckpointFactsForRecord projects the authoritative readiness of the selected
// CheckpointRecord. It is a faithful copy: no field is added to make the result
// look safer than the record says.
func CheckpointFactsForRecord(
	record *aevrelv1.CheckpointRecord,
	candidates int,
	selected string,
	opts FactsOptions,
) CheckpointFacts {
	now := opts.clock()
	readiness := record.Status.Readiness
	complete := readiness.Complete
	fresh := readiness.Fresh

	envelopeReasons := make([]string, 0, 1)
	if opts.MaxObservationAge > 0 &&
		observationAge(readiness.ObservedAt, now) > opts.MaxObservationAge {
		// Downgrade only. The control plane owns freshness; the envelope may
		// withdraw it when the observation carrying it is too old to be a
		// statement about now.
		fresh = false
		envelopeReasons = append(envelopeReasons, FactReasonStaleObservation)
	}

	decision := control.DecideRescue(
		rescueEventAdvisory,
		"",
		string(control.RescueHold),
		controlReadinessForDecision(readiness, fresh),
		now,
	)
	facts := CheckpointFacts{
		SchemaVersion:       SchemaVersion,
		Status:              readiness.Status,
		Complete:            factBoolPtr(complete),
		Fresh:               factBoolPtr(fresh),
		Loadable:            readiness.Loadable,
		Movable:             readiness.Movable,
		ObservedAt:          readiness.ObservedAt,
		Source:              FactSourceCheckpointRecord,
		ReasonCodes:         factReasonCodes(readiness.Reasons),
		EnvelopeReasons:     envelopeReasons,
		RestoreMode:         string(decision.Action),
		RestoreReasonCode:   decision.Code,
		RestoreReason:       decision.Reason,
		EstimatedRTOSeconds: readiness.EstimatedRTOSeconds,
		SelectedRecord:      selected,
		Candidates:          candidates,
		TargetWorldSize:     readiness.TargetWorldSize,
		LastDrill:           readiness.LastDrill,
		LastDrillVersion:    readiness.LastDrillVersion,
		LastDrillDigest:     readiness.LastDrillDigest,
		StateComplete:       readiness.StateComplete,
		ReadinessLevel:      readiness.ReadinessLevel,
	}
	facts.Conformance = ValidateFacts(facts)
	return facts
}

// CheckpointFactsForAmbiguity describes a selection that could not be resolved.
// More than one CheckpointRecord matches and none is unambiguously latest, so
// there is no fact to publish. The action is still derived from DecideRescue:
// an unresolved inventory is an incomplete one.
func CheckpointFactsForAmbiguity(candidates int, opts FactsOptions) CheckpointFacts {
	decision := control.DecideRescue(
		rescueEventAdvisory,
		"",
		string(control.RescueHold),
		&control.CheckpointReadiness{Complete: false},
		opts.clock(),
	)
	facts := CheckpointFacts{
		SchemaVersion:     SchemaVersion,
		Source:            FactSourceCheckpointRecordAmbiguous,
		ReasonCodes:       []string{},
		EnvelopeReasons:   []string{FactReasonAmbiguousRecords},
		RestoreMode:       string(decision.Action),
		RestoreReasonCode: decision.Code,
		RestoreReason:     decision.Reason,
		Candidates:        candidates,
		Ambiguous:         true,
	}
	facts.Conformance = ValidateFacts(facts)
	return facts
}

// CheckpointFactsForAnnotations is the envelope for a workload that opted in
// but has no CheckpointRecord. Annotation values never become facts: a label is
// only ever a projection of authoritative readiness, so this fact set carries
// the source and the fail-closed action and nothing else.
func CheckpointFactsForAnnotations(invalid bool, opts FactsOptions) CheckpointFacts {
	decision := control.DecideRescue(
		rescueEventAdvisory,
		"",
		string(control.RescueHold),
		nil,
		opts.clock(),
	)
	envelopeReasons := []string{FactReasonAnnotationFallback}
	if invalid {
		envelopeReasons = append(envelopeReasons, FactReasonAnnotationInvalid)
	}
	facts := CheckpointFacts{
		SchemaVersion:     SchemaVersion,
		Source:            FactSourceAnnotationFallback,
		ReasonCodes:       []string{},
		EnvelopeReasons:   envelopeReasons,
		RestoreMode:       string(decision.Action),
		RestoreReasonCode: decision.Code,
		RestoreReason:     decision.Reason,
	}
	facts.Conformance = ValidateFacts(facts)
	return facts
}

// CheckpointFactsForNoSignal is the envelope for an opted-in workload that
// published nothing at all. We do not know anything, and the fact set says so.
func CheckpointFactsForNoSignal(opts FactsOptions) CheckpointFacts {
	decision := control.DecideRescue(
		rescueEventAdvisory,
		"",
		string(control.RescueHold),
		nil,
		opts.clock(),
	)
	facts := CheckpointFacts{
		SchemaVersion:     SchemaVersion,
		Source:            FactSourceUnknown,
		ReasonCodes:       []string{},
		EnvelopeReasons:   []string{FactReasonNoCheckpointRecord},
		RestoreMode:       string(decision.Action),
		RestoreReasonCode: decision.Code,
		RestoreReason:     decision.Reason,
	}
	facts.Conformance = ValidateFacts(facts)
	return facts
}

// controlReadinessForDecision converts the API readiness status into the
// control-plane type DecideRescue consumes. It is a field-for-field mapping;
// freshness is passed separately because the envelope may have downgraded it.
func controlReadinessForDecision(
	readiness aevrelv1.CheckpointReadinessStatus,
	fresh bool,
) *control.CheckpointReadiness {
	reasons := make([]control.Reason, 0, len(readiness.Reasons))
	for _, reason := range readiness.Reasons {
		reasons = append(reasons, control.Reason{
			Code:   reason.Code,
			Detail: reason.Detail,
		})
	}
	var targetWorldSize *int
	if readiness.TargetWorldSize != nil {
		value := int(*readiness.TargetWorldSize)
		targetWorldSize = &value
	}
	return &control.CheckpointReadiness{
		Status:              control.ReadinessStatus(readiness.Status),
		Complete:            readiness.Complete,
		Fresh:               fresh,
		Loadable:            readiness.Loadable,
		Movable:             readiness.Movable,
		StateComplete:       readiness.StateComplete,
		ReadinessLevel:      readiness.ReadinessLevel,
		Reasons:             reasons,
		ObservedAt:          readiness.ObservedAt,
		TargetWorldSize:     targetWorldSize,
		EstimatedRTOSeconds: readiness.EstimatedRTOSeconds,
		LastDrill:           readiness.LastDrill,
		LastDrillDigest:     readiness.LastDrillDigest,
	}
}

// factReasonCodes copies the readiness reasons unchanged. Order is preserved so
// an envelope and the readiness layer it came from can be compared directly.
func factReasonCodes(reasons []aevrelv1.ControlReason) []string {
	codes := make([]string, 0, len(reasons))
	for _, reason := range reasons {
		codes = append(codes, reason.Code)
	}
	return codes
}

func observationAge(observedAt string, now time.Time) time.Duration {
	if observedAt == "" {
		return 0
	}
	observed, err := time.Parse(time.RFC3339, observedAt)
	if err != nil {
		return 0
	}
	age := now.Sub(observed)
	if age < 0 {
		return 0
	}
	return age
}

func factBoolPtr(value bool) *bool {
	return &value
}
