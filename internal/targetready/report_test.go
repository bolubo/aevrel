package targetready

import (
	"strings"
	"testing"
)

func TestEvaluateRequiresEveryRankToPass(t *testing.T) {
	reports := []RankReport{
		completeReport(0),
		completeReport(1),
	}
	result := Evaluate(EvaluateOptions{
		Reports:              reports,
		ExpectedRanks:        []int32{0, 1},
		Version:              "step-100",
		MinSteps:             10,
		RequireStateComplete: true,
	})
	if !result.Ready || result.Condition != "Ready" {
		t.Fatalf("evaluation = %+v", result)
	}
	if result.RanksReady != 2 || result.StepsRan != 10 {
		t.Fatalf("evaluation counts = %+v", result)
	}
	if !result.StateComplete {
		t.Fatalf("state completeness = %+v", result)
	}
	if len(result.ReasonCodes) != 0 {
		t.Fatalf("unexpected reason codes = %v", result.ReasonCodes)
	}
}

func TestEvaluateFailsClosedOnMissingRank(t *testing.T) {
	result := Evaluate(EvaluateOptions{
		Reports:       []RankReport{completeReport(0)},
		ExpectedRanks: []int32{0, 1},
		Version:       "step-100",
		MinSteps:      10,
	})
	if result.Ready || result.Condition != "Incomplete" {
		t.Fatalf("evaluation = %+v", result)
	}
	if len(result.MissingRanks) != 1 || result.MissingRanks[0] != 1 {
		t.Fatalf("missing ranks = %v", result.MissingRanks)
	}
	if result.StateComplete {
		t.Fatal("a run with a missing rank claimed complete state")
	}
}

func TestEvaluateRejectsVersionAndLoadAndStepFailures(t *testing.T) {
	versionMismatch := completeReport(0)
	versionMismatch.Version = "step-99"
	if got := Evaluate(EvaluateOptions{
		Reports:       []RankReport{versionMismatch},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	}); got.Condition != "VersionMismatch" {
		t.Fatalf("version condition = %+v", got)
	}

	loadFailure := completeReport(0)
	loadFailure.OptimizerLoaded = false
	if got := Evaluate(EvaluateOptions{
		Reports:       []RankReport{loadFailure},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	}); got.Condition != "LoadFailed" {
		t.Fatalf("load condition = %+v", got)
	}

	stepFailure := completeReport(0)
	stepFailure.StepsRan = 9
	if got := Evaluate(EvaluateOptions{
		Reports:       []RankReport{stepFailure},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	}); got.Condition != "StepFailed" {
		t.Fatalf("step condition = %+v", got)
	}
}

func TestEvaluateKeepsPartialClosureAsL3(t *testing.T) {
	report := partialReport(0)
	result := Evaluate(EvaluateOptions{
		Reports:       []RankReport{report},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	})
	if !result.Ready || result.Condition != "Ready" {
		t.Fatalf("L3 evaluation = %+v", result)
	}
	if result.StateComplete {
		t.Fatalf("partial closure claimed completeness: %+v", result)
	}
	if len(result.ReasonCodes) != 1 ||
		result.ReasonCodes[0] != ReasonComponentMissing {
		t.Fatalf("reason codes = %v", result.ReasonCodes)
	}
	if result.RanksReady != 1 {
		t.Fatalf("L3 rank count = %+v", result)
	}
}

func TestEvaluateFailsClosedWhenClosureIsRequired(t *testing.T) {
	report := partialReport(0)
	result := Evaluate(EvaluateOptions{
		Reports:              []RankReport{report},
		ExpectedRanks:        []int32{0},
		Version:              "step-100",
		MinSteps:             10,
		RequireStateComplete: true,
	})
	if result.Ready || result.Condition != "StateClosureIncomplete" {
		t.Fatalf("L4 evaluation = %+v", result)
	}
	if result.RanksReady != 0 {
		t.Fatalf("L4 counted passing ranks = %+v", result)
	}
	if !strings.Contains(result.Message, ReasonComponentMissing) {
		t.Fatalf("message lost the reason code: %q", result.Message)
	}
}

func TestEvaluateRejectsDivergentManifestDigests(t *testing.T) {
	first := completeReport(0)
	second := completeReport(1)
	other := "sha256:" + strings.Repeat("1", 64)
	second.ResumeState = &ResumeStateEvidence{
		ManifestDigest: other,
		Closure:        ClosureComplete,
		Components:     completeComponents(),
	}
	result := Evaluate(EvaluateOptions{
		Reports:              []RankReport{first, second},
		ExpectedRanks:        []int32{0, 1},
		Version:              "step-100",
		MinSteps:             10,
		RequireStateComplete: true,
	})
	if result.Ready || result.Condition != "InvalidReport" {
		t.Fatalf("evaluation = %+v", result)
	}
	if !strings.Contains(result.Message, "different resume-state manifests") {
		t.Fatalf("message = %q", result.Message)
	}
	if result.StateComplete {
		t.Fatal("divergent manifests claimed complete state")
	}
}

func TestEvaluateTreatsLegacyClaimsAsUnverified(t *testing.T) {
	report := validLegacyReport(0)
	report.StateComplete = true
	result := Evaluate(EvaluateOptions{
		Reports:       []RankReport{report},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	})
	if !result.Ready {
		t.Fatalf("legacy L3 evaluation = %+v", result)
	}
	if result.StateComplete {
		t.Fatal("a legacy boolean was accepted as closure evidence")
	}
	if len(result.ReasonCodes) != 1 ||
		result.ReasonCodes[0] != ReasonStateUnverified {
		t.Fatalf("reason codes = %v", result.ReasonCodes)
	}

	gated := Evaluate(EvaluateOptions{
		Reports:              []RankReport{report},
		ExpectedRanks:        []int32{0},
		Version:              "step-100",
		MinSteps:             10,
		RequireStateComplete: true,
	})
	if gated.Ready || gated.Condition != "StateClosureIncomplete" {
		t.Fatalf("legacy L4 evaluation = %+v", gated)
	}
}

func TestEvaluateReportsMalformedClosure(t *testing.T) {
	report := completeReport(0)
	report.StateComplete = false
	report.ResumeState = &ResumeStateEvidence{
		ManifestDigest: "sha256:" + strings.Repeat("0", 64),
		Closure:        ClosureMalformed,
		Components:     nil,
	}
	result := Evaluate(EvaluateOptions{
		Reports:       []RankReport{report},
		ExpectedRanks: []int32{0},
		Version:       "step-100",
		MinSteps:      10,
	})
	if len(result.ReasonCodes) != 1 ||
		result.ReasonCodes[0] != ReasonManifestInvalid {
		t.Fatalf("reason codes = %v", result.ReasonCodes)
	}
}

func TestRankReportValidateRequiresEvidence(t *testing.T) {
	report := completeReport(0)
	report.ResumeState = nil
	if err := report.Validate(); err == nil ||
		!strings.Contains(err.Error(), "requires resumeState") {
		t.Fatalf("missing evidence error = %v", err)
	}

	disagreeing := completeReport(0)
	disagreeing.StateComplete = false
	if err := disagreeing.Validate(); err == nil ||
		!strings.Contains(err.Error(), "disagrees") {
		t.Fatalf("disagreement error = %v", err)
	}

	badDigest := completeReport(0)
	badDigest.ResumeState = &ResumeStateEvidence{
		ManifestDigest: "not-a-digest",
		Closure:        ClosureComplete,
	}
	if err := badDigest.Validate(); err == nil ||
		!strings.Contains(err.Error(), "manifestDigest") {
		t.Fatalf("digest error = %v", err)
	}

	legacy := validLegacyReport(0)
	legacy.ResumeState = &ResumeStateEvidence{
		ManifestDigest: "sha256:" + strings.Repeat("0", 64),
		Closure:        ClosureComplete,
	}
	if err := legacy.Validate(); err == nil ||
		!strings.Contains(err.Error(), "must not carry resumeState") {
		t.Fatalf("legacy evidence error = %v", err)
	}
}

func TestDecodeRankReportRejectsUnknownFields(t *testing.T) {
	payload := []byte(`{
		"schemaVersion":"1",
		"migrationUID":"default/migration",
		"version":"step-100",
		"rank":0,
		"role":"worker",
		"modelLoaded":true,
		"optimizerLoaded":true,
		"stepsRan":10,
		"stateComplete":false,
		"ok":true,
		"error":"",
		"surprise":true
	}`)
	if _, err := DecodeRankReport(payload); err == nil ||
		!strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("decode error = %v", err)
	}
}

func TestDecodeRankReportRejectsMalformedJSON(t *testing.T) {
	if _, err := DecodeRankReport([]byte(`{"ok": true`)); err == nil {
		t.Fatal("malformed report was accepted")
	}
}

func TestDecodeRankReportNormalizesComponents(t *testing.T) {
	payload := []byte(`{
		"schemaVersion":"2",
		"migrationUID":"default/migration",
		"version":"step-100",
		"rank":0,
		"role":"worker",
		"modelLoaded":true,
		"optimizerLoaded":true,
		"stepsRan":10,
		"stateComplete":true,
		"ok":true,
		"error":"",
		"resumeState":{
			"manifestDigest":"sha256:` + strings.Repeat("0", 64) + `",
			"closure":"COMPLETE",
			"components":[
				{"kind":"rng","required":true,"present":true},
				{"kind":"model","required":true,"present":true}
			]
		}
	}`)
	report, err := DecodeRankReport(payload)
	if err != nil {
		t.Fatal(err)
	}
	if report.ResumeState.Components[0].Kind != "model" {
		t.Fatalf("components not canonical: %+v", report.ResumeState.Components)
	}
}

func TestDecodeRankReportRejectsLegacyWithoutEvidence(t *testing.T) {
	payload := []byte(`{
		"schemaVersion":"2",
		"migrationUID":"default/migration",
		"version":"step-100",
		"rank":0,
		"role":"worker",
		"modelLoaded":true,
		"optimizerLoaded":true,
		"stepsRan":10,
		"stateComplete":false,
		"ok":true,
		"error":""
	}`)
	if _, err := DecodeRankReport(payload); err == nil ||
		!strings.Contains(err.Error(), "requires resumeState") {
		t.Fatalf("decode error = %v", err)
	}
}

func TestFailureAggregateMarshalsEmptyReportsAsArray(t *testing.T) {
	aggregate := NewAggregate(
		"default/migration",
		"step-100",
		"validate",
		"2026-10-22T00:00:00Z",
		Evaluation{
			Condition:   "JobFailed",
			Message:     "target failed",
			ReasonCodes: []string{ReasonComponentMissing},
			RanksTotal:  2,
		},
	)
	payload, err := aggregate.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	text := string(payload)
	if !strings.Contains(text, `"reports": []`) {
		t.Fatalf("aggregate reports is not an array:\n%s", text)
	}
	if !strings.Contains(text, `"condition": "JobFailed"`) {
		t.Fatalf("aggregate condition missing:\n%s", text)
	}
	if !strings.Contains(text, `"reasonCodes": [`) {
		t.Fatalf("aggregate reason codes missing:\n%s", text)
	}
	if !strings.Contains(text, `"schemaVersion": "2"`) {
		t.Fatalf("aggregate schema version missing:\n%s", text)
	}
}

func completeComponents() []ResumeComponentEvidence {
	return []ResumeComponentEvidence{
		{Kind: "model", Required: true, Present: true},
		{Kind: "optimizer", Required: true, Present: true},
		{Kind: "global_step", Required: true, Present: true},
		{Kind: "dataloader", Required: true, Present: true},
		{Kind: "rng", Required: true, Present: true},
	}
}

func completeReport(rank int32) RankReport {
	return RankReport{
		SchemaVersion:   SchemaVersion,
		MigrationUID:    "default/migration",
		Version:         "step-100",
		Rank:            rank,
		Role:            "worker",
		ModelLoaded:     true,
		OptimizerLoaded: true,
		StepsRan:        10,
		StateComplete:   true,
		ResumeState: &ResumeStateEvidence{
			ManifestDigest: "sha256:" + strings.Repeat("0", 64),
			Closure:        ClosureComplete,
			Components:     completeComponents(),
		},
		OK: true,
	}
}

func partialReport(rank int32) RankReport {
	components := completeComponents()
	components[3].Present = false
	return RankReport{
		SchemaVersion:   SchemaVersion,
		MigrationUID:    "default/migration",
		Version:         "step-100",
		Rank:            rank,
		Role:            "worker",
		ModelLoaded:     true,
		OptimizerLoaded: true,
		StepsRan:        10,
		StateComplete:   false,
		ResumeState: &ResumeStateEvidence{
			ManifestDigest: "sha256:" + strings.Repeat("0", 64),
			Closure:        ClosurePartial,
			Components:     components,
		},
		OK: true,
	}
}

func validLegacyReport(rank int32) RankReport {
	return RankReport{
		SchemaVersion:   LegacySchemaVersion,
		MigrationUID:    "default/migration",
		Version:         "step-100",
		Rank:            rank,
		Role:            "worker",
		ModelLoaded:     true,
		OptimizerLoaded: true,
		StepsRan:        10,
		OK:              true,
	}
}
