package targetready

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

const SchemaVersion = "1"

// RankReport is the per-rank contract written by a target validation workload.
type RankReport struct {
	SchemaVersion   string `json:"schemaVersion"`
	MigrationUID    string `json:"migrationUID"`
	Version         string `json:"version"`
	Rank            int32  `json:"rank"`
	Role            string `json:"role"`
	ModelLoaded     bool   `json:"modelLoaded"`
	OptimizerLoaded bool   `json:"optimizerLoaded"`
	StepsRan        int32  `json:"stepsRan"`
	StateComplete   bool   `json:"stateComplete"`
	OK              bool   `json:"ok"`
	Error           string `json:"error"`
}

func (r RankReport) Validate() error {
	if r.SchemaVersion != SchemaVersion {
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
	if err := report.Validate(); err != nil {
		return RankReport{}, err
	}
	return report, nil
}

// Evaluation is the fail-closed aggregate of all target rank reports.
type Evaluation struct {
	Ready         bool
	Condition     string
	Message       string
	RanksReady    int32
	RanksTotal    int32
	StepsRan      int32
	StateComplete bool
	Reports       []RankReport
	MissingRanks  []int32
}

// Evaluate requires every expected rank exactly once, the same version,
// model and optimizer loads, and at least minSteps on every rank.
func Evaluate(
	reports []RankReport,
	expectedRanks []int32,
	version string,
	minSteps int32,
) Evaluation {
	result := Evaluation{
		RanksTotal: int32(len(expectedRanks)),
		Reports:    append([]RankReport(nil), reports...),
	}
	sort.Slice(result.Reports, func(i, j int) bool {
		return result.Reports[i].Rank < result.Reports[j].Rank
	})

	expected := make(map[int32]struct{}, len(expectedRanks))
	for _, rank := range expectedRanks {
		expected[rank] = struct{}{}
	}
	seen := make(map[int32]RankReport, len(reports))
	var invalid []string
	var duplicates []int32
	var extras []int32
	for _, report := range reports {
		if err := report.Validate(); err != nil {
			invalid = append(invalid, fmt.Sprintf("rank %d: %v", report.Rank, err))
			continue
		}
		if _, ok := expected[report.Rank]; !ok {
			extras = append(extras, report.Rank)
			continue
		}
		if previous, ok := seen[report.Rank]; ok {
			if previous != report {
				duplicates = append(duplicates, report.Rank)
			}
			continue
		}
		seen[report.Rank] = report
	}
	for _, rank := range expectedRanks {
		if _, ok := seen[rank]; !ok {
			result.MissingRanks = append(result.MissingRanks, rank)
		}
	}
	sort.Slice(result.MissingRanks, func(i, j int) bool {
		return result.MissingRanks[i] < result.MissingRanks[j]
	})
	sort.Slice(duplicates, func(i, j int) bool { return duplicates[i] < duplicates[j] })
	sort.Slice(extras, func(i, j int) bool { return extras[i] < extras[j] })

	if len(reports) > 0 {
		result.StepsRan = reports[0].StepsRan
		result.StateComplete = reports[0].StateComplete
	}
	for index, report := range result.Reports {
		if index > 0 && report.StepsRan < result.StepsRan {
			result.StepsRan = report.StepsRan
		}
		result.StateComplete = result.StateComplete && report.StateComplete
		if targetRankPasses(report, version, minSteps) {
			result.RanksReady++
		}
	}
	if len(reports) == 0 {
		result.StateComplete = false
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
	case hasVersionMismatch(seen, version):
		result.Condition = "VersionMismatch"
		result.Message = fmt.Sprintf(
			"target ranks did not report version %q",
			version,
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
	case hasStepFailure(seen, minSteps):
		result.Condition = "StepFailed"
		result.Message = fmt.Sprintf(
			"one or more target ranks ran fewer than %d steps",
			minSteps,
		)
	default:
		result.Ready = true
		result.Condition = "Ready"
		result.Message = fmt.Sprintf(
			"all %d target ranks loaded model/optimizer and ran at least %d steps",
			result.RanksTotal,
			minSteps,
		)
	}
	return result
}

func targetRankPasses(report RankReport, version string, minSteps int32) bool {
	return report.Validate() == nil &&
		report.Version == version &&
		report.OK &&
		report.ModelLoaded &&
		report.OptimizerLoaded &&
		report.StepsRan >= minSteps
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
