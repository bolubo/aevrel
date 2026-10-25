package targetready

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
)

const (
	// SchemaVersion is the current per-rank contract. Version 2 binds every
	// readiness claim to a resume-state manifest digest.
	SchemaVersion = "2"
	// LegacySchemaVersion stays decodable so a stale target produces a precise
	// validation error instead of an opaque decode error.
	LegacySchemaVersion = "1"
)

// Resume-state closure values, mirroring spec/v1/resume-state.schema.json.
const (
	ClosureComplete  = "COMPLETE"
	ClosurePartial   = "PARTIAL"
	ClosureMissing   = "MISSING"
	ClosureMalformed = "MALFORMED"
)

// Reason codes a rank contributes when it cannot prove closure.
const (
	ReasonComponentMissing = "RESUME_COMPONENT_MISSING"
	ReasonManifestInvalid  = "RESUME_MANIFEST_INVALID"
	ReasonStateUnverified  = "RESUME_STATE_UNVERIFIED"
)

var (
	digestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	// The control plane is the authority on the contract, so it accepts the
	// legacy version only to reject it with a readable message.
	supportedSchemaVersions = map[string]struct{}{
		LegacySchemaVersion: {},
		SchemaVersion:       {},
	}
)

// ResumeComponentEvidence is one declared resume component as the rank saw it.
type ResumeComponentEvidence struct {
	Kind     string `json:"kind"`
	Required bool   `json:"required"`
	Present  bool   `json:"present"`
}

// ResumeStateEvidence binds a readiness claim to one resume-state manifest.
//
// The manifest digest is the only binding point: component digests stay in the
// manifest, so a report can never disagree with itself.
type ResumeStateEvidence struct {
	ManifestDigest string                    `json:"manifestDigest"`
	Closure        string                    `json:"closure"`
	Components     []ResumeComponentEvidence `json:"components"`
}

func (e ResumeStateEvidence) Validate() error {
	if !digestPattern.MatchString(e.ManifestDigest) {
		return fmt.Errorf("resumeState.manifestDigest must be sha256")
	}
	switch e.Closure {
	case ClosureComplete, ClosurePartial, ClosureMissing, ClosureMalformed:
	default:
		return fmt.Errorf("unsupported resumeState closure %q", e.Closure)
	}
	seen := make(map[string]struct{}, len(e.Components))
	for _, component := range e.Components {
		if strings.TrimSpace(component.Kind) == "" {
			return fmt.Errorf("resumeState component kind is required")
		}
		if _, ok := seen[component.Kind]; ok {
			return fmt.Errorf("duplicate resumeState component %q", component.Kind)
		}
		seen[component.Kind] = struct{}{}
	}
	return nil
}

// Complete reports whether the manifest proved every required component.
func (e ResumeStateEvidence) Complete() bool {
	return e.Closure == ClosureComplete
}

// MissingRequired lists required components the snapshot did not contain.
func (e ResumeStateEvidence) MissingRequired() []string {
	missing := make([]string, 0, len(e.Components))
	for _, component := range e.Components {
		if component.Required && !component.Present {
			missing = append(missing, component.Kind)
		}
	}
	sort.Strings(missing)
	return missing
}

// sorted returns a copy with components in canonical order so two equal claims
// always encode identically.
func (e ResumeStateEvidence) sorted() ResumeStateEvidence {
	if len(e.Components) < 2 {
		return e
	}
	components := append([]ResumeComponentEvidence(nil), e.Components...)
	sort.Slice(components, func(i, j int) bool {
		return components[i].Kind < components[j].Kind
	})
	e.Components = components
	return e
}

// RankReport is the per-rank contract written by a target validation workload.
type RankReport struct {
	SchemaVersion   string               `json:"schemaVersion"`
	MigrationUID    string               `json:"migrationUID"`
	Version         string               `json:"version"`
	Rank            int32                `json:"rank"`
	Role            string               `json:"role"`
	ModelLoaded     bool                 `json:"modelLoaded"`
	OptimizerLoaded bool                 `json:"optimizerLoaded"`
	StepsRan        int32                `json:"stepsRan"`
	StateComplete   bool                 `json:"stateComplete"`
	ResumeState     *ResumeStateEvidence `json:"resumeState,omitempty"`
	OK              bool                 `json:"ok"`
	Error           string               `json:"error"`
}

func (r RankReport) Validate() error {
	if _, ok := supportedSchemaVersions[r.SchemaVersion]; !ok {
		return fmt.Errorf(
			"unsupported target-ready schemaVersion %q",
			r.SchemaVersion,
		)
	}
	if r.MigrationUID == "" {
		return fmt.Errorf("target-ready migrationUID is required")
	}
	if r.Version == "" {
		return fmt.Errorf("target-ready version is required")
	}
	if r.Rank < 0 {
		return fmt.Errorf("target-ready rank must be non-negative")
	}
	if strings.TrimSpace(r.Role) == "" {
		return fmt.Errorf("target-ready role is required")
	}
	if r.StepsRan < 0 {
		return fmt.Errorf("target-ready stepsRan must be non-negative")
	}
	if r.OK && r.Error != "" {
		return fmt.Errorf("target-ready report is ok but has an error")
	}
	if r.SchemaVersion == SchemaVersion {
		if r.ResumeState == nil {
			return fmt.Errorf(
				"target-ready schemaVersion %q requires resumeState evidence",
				SchemaVersion,
			)
		}
		if err := r.ResumeState.Validate(); err != nil {
			return fmt.Errorf("rank %d %w", r.Rank, err)
		}
		if r.StateComplete != r.ResumeState.Complete() {
			return fmt.Errorf(
				"rank %d stateComplete disagrees with resumeState.closure %q",
				r.Rank,
				r.ResumeState.Closure,
			)
		}
		return nil
	}
	if r.ResumeState != nil {
		return fmt.Errorf(
			"target-ready schemaVersion %q must not carry resumeState evidence",
			LegacySchemaVersion,
		)
	}
	return nil
}

// DecodeRankReport rejects unknown fields, trailing JSON and incomplete reports.
func DecodeRankReport(payload []byte) (RankReport, error) {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	var report RankReport
	if err := decoder.Decode(&report); err != nil {
		return RankReport{}, fmt.Errorf("decode target-ready report: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return RankReport{}, fmt.Errorf(
				"target-ready report contains trailing JSON",
			)
		}
		return RankReport{}, fmt.Errorf(
			"decode trailing target-ready data: %w",
			err,
		)
	}
	if report.ResumeState != nil {
		normalized := report.ResumeState.sorted()
		report.ResumeState = &normalized
	}
	if err := report.Validate(); err != nil {
		return RankReport{}, err
	}
	return report, nil
}

// EvaluateOptions carries every input of a target evaluation. A single struct
// keeps call sites explicit as the contract grows.
type EvaluateOptions struct {
	Reports       []RankReport
	ExpectedRanks []int32
	Version       string
	MinSteps      int32
	// RequireStateComplete turns the evaluation into an L4 claim: the run is
	// only ready when every expected rank proved complete resume state.
	RequireStateComplete bool
}

// Evaluation is the fail-closed aggregate of all target rank reports.
type Evaluation struct {
	Ready         bool
	Condition     string
	Message       string
	ReasonCodes   []string
	RanksReady    int32
	RanksTotal    int32
	StepsRan      int32
	StateComplete bool
	Reports       []RankReport
	MissingRanks  []int32
}

// Evaluate requires every expected rank exactly once, the same version, model
// and optimizer loads, and at least minSteps on every rank. When a state
// closure is required, an unproven closure fails the run closed.
func Evaluate(opts EvaluateOptions) Evaluation {
	result := Evaluation{
		RanksTotal: int32(len(opts.ExpectedRanks)),
		Reports:    append([]RankReport(nil), opts.Reports...),
	}
	sort.Slice(result.Reports, func(i, j int) bool {
		return result.Reports[i].Rank < result.Reports[j].Rank
	})

	expected := make(map[int32]struct{}, len(opts.ExpectedRanks))
	for _, rank := range opts.ExpectedRanks {
		expected[rank] = struct{}{}
	}
	seen := make(map[int32]RankReport, len(opts.Reports))
	var invalid []string
	var duplicates []int32
	var extras []int32
	for _, report := range opts.Reports {
		if err := report.Validate(); err != nil {
			invalid = append(invalid, fmt.Sprintf("rank %d: %v", report.Rank, err))
			continue
		}
		if _, ok := expected[report.Rank]; !ok {
			extras = append(extras, report.Rank)
			continue
		}
		if previous, ok := seen[report.Rank]; ok {
			if !rankReportEqual(previous, report) {
				duplicates = append(duplicates, report.Rank)
			}
			continue
		}
		seen[report.Rank] = report
	}
	for _, rank := range opts.ExpectedRanks {
		if _, ok := seen[rank]; !ok {
			result.MissingRanks = append(result.MissingRanks, rank)
		}
	}
	sort.Slice(result.MissingRanks, func(i, j int) bool {
		return result.MissingRanks[i] < result.MissingRanks[j]
	})
	sort.Slice(duplicates, func(i, j int) bool { return duplicates[i] < duplicates[j] })
	sort.Slice(extras, func(i, j int) bool { return extras[i] < extras[j] })

	stateComplete, reasonCodes, manifestMismatch := stateClosure(
		opts.ExpectedRanks,
		seen,
	)
	result.StateComplete = stateComplete
	result.ReasonCodes = reasonCodes
	result.StepsRan = minStepsRan(result.Reports)
	for _, report := range result.Reports {
		if targetRankPasses(report, opts.Version, opts.MinSteps, opts.RequireStateComplete) {
			result.RanksReady++
		}
	}

	switch {
	case len(invalid) > 0:
		result.Condition = "InvalidReport"
		result.Message = "invalid target-ready reports: " + strings.Join(invalid, "; ")
	case len(duplicates) > 0:
		result.Condition = "Incomplete"
		result.Message = fmt.Sprintf(
			"conflicting target-ready reports for ranks %v",
			duplicates,
		)
	case len(extras) > 0:
		result.Condition = "Incomplete"
		result.Message = fmt.Sprintf(
			"unexpected target-ready ranks %v",
			extras,
		)
	case manifestMismatch:
		result.Condition = "InvalidReport"
		result.Message = "target ranks reported different resume-state manifests"
	case hasVersionMismatch(seen, opts.Version):
		result.Condition = "VersionMismatch"
		result.Message = fmt.Sprintf(
			"target ranks did not report version %q",
			opts.Version,
		)
	case len(result.MissingRanks) > 0:
		result.Condition = "Incomplete"
		result.Message = fmt.Sprintf(
			"missing target-ready reports for ranks %v",
			result.MissingRanks,
		)
	case hasLoadFailure(seen):
		result.Condition = "LoadFailed"
		result.Message = "one or more target ranks did not load model/optimizer"
	case hasStepFailure(seen, opts.MinSteps):
		result.Condition = "StepFailed"
		result.Message = fmt.Sprintf(
			"one or more target ranks ran fewer than %d steps",
			opts.MinSteps,
		)
	case opts.RequireStateComplete && !result.StateComplete:
		result.Condition = "StateClosureIncomplete"
		result.Message = stateClosureMessage(result.ReasonCodes)
	default:
		result.Ready = true
		result.Condition = "Ready"
		result.Message = fmt.Sprintf(
			"all %d target ranks loaded model/optimizer and ran at least %d steps",
			result.RanksTotal,
			opts.MinSteps,
		)
	}
	return result
}

// stateClosure proves closure across the expected ranks. A rank without
// evidence can never count as complete, and ranks that restored different
// manifests void the claim entirely.
func stateClosure(
	expectedRanks []int32,
	seen map[int32]RankReport,
) (bool, []string, bool) {
	complete := len(expectedRanks) > 0
	codes := make(map[string]struct{}, 3)
	digests := make(map[string]struct{}, 1)
	for _, rank := range expectedRanks {
		report, ok := seen[rank]
		if !ok {
			complete = false
			continue
		}
		if report.ResumeState == nil {
			complete = false
			codes[ReasonStateUnverified] = struct{}{}
			continue
		}
		digests[report.ResumeState.ManifestDigest] = struct{}{}
		if report.ResumeState.Complete() {
			continue
		}
		complete = false
		switch report.ResumeState.Closure {
		case ClosureMalformed:
			codes[ReasonManifestInvalid] = struct{}{}
		default:
			if len(report.ResumeState.MissingRequired()) > 0 {
				codes[ReasonComponentMissing] = struct{}{}
			} else {
				codes[ReasonStateUnverified] = struct{}{}
			}
		}
	}
	manifestMismatch := len(digests) > 1
	if manifestMismatch {
		complete = false
		codes[ReasonManifestInvalid] = struct{}{}
	}
	return complete, sortedCodes(codes), manifestMismatch
}

func sortedCodes(codes map[string]struct{}) []string {
	if len(codes) == 0 {
		return nil
	}
	result := make([]string, 0, len(codes))
	for code := range codes {
		result = append(result, code)
	}
	sort.Strings(result)
	return result
}

func stateClosureMessage(codes []string) string {
	if len(codes) == 0 {
		return "target ranks did not prove complete resume state"
	}
	return "target ranks did not prove complete resume state: " +
		strings.Join(codes, ", ")
}

func minStepsRan(reports []RankReport) int32 {
	if len(reports) == 0 {
		return 0
	}
	result := reports[0].StepsRan
	for _, report := range reports[1:] {
		if report.StepsRan < result {
			result = report.StepsRan
		}
	}
	return result
}

func rankReportEqual(left RankReport, right RankReport) bool {
	leftPayload, errLeft := json.Marshal(left)
	rightPayload, errRight := json.Marshal(right)
	if errLeft != nil || errRight != nil {
		return false
	}
	return bytes.Equal(leftPayload, rightPayload)
}

func targetRankPasses(
	report RankReport,
	version string,
	minSteps int32,
	requireState bool,
) bool {
	if report.Validate() != nil {
		return false
	}
	if !report.OK || !report.ModelLoaded || !report.OptimizerLoaded {
		return false
	}
	if report.Version != version || report.StepsRan < minSteps {
		return false
	}
	if requireState && (report.ResumeState == nil || !report.ResumeState.Complete()) {
		return false
	}
	return true
}

func hasVersionMismatch(reports map[int32]RankReport, version string) bool {
	for _, report := range reports {
		if report.Version != version {
			return true
		}
	}
	return false
}

func hasLoadFailure(reports map[int32]RankReport) bool {
	for _, report := range reports {
		if !report.OK || !report.ModelLoaded || !report.OptimizerLoaded {
			return true
		}
	}
	return false
}

func hasStepFailure(reports map[int32]RankReport, minSteps int32) bool {
	for _, report := range reports {
		if report.StepsRan < minSteps {
			return true
		}
	}
	return false
}
