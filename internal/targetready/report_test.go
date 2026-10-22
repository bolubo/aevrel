package targetready

import (
	"strings"
	"testing"
)

func TestEvaluateRequiresEveryRankToPass(t *testing.T) {
	reports := []RankReport{
		validReport(0),
		validReport(1),
	}
	result := Evaluate(reports, []int32{0, 1}, "step-100", 10)
	if !result.Ready || result.Condition != "Ready" {
		t.Fatalf("evaluation = %+v", result)
	}
	if result.RanksReady != 2 || result.StepsRan != 10 {
		t.Fatalf("evaluation counts = %+v", result)
	}
}

func TestEvaluateFailsClosedOnMissingRank(t *testing.T) {
	result := Evaluate(
		[]RankReport{validReport(0)},
		[]int32{0, 1},
		"step-100",
		10,
	)
	if result.Ready || result.Condition != "Incomplete" {
		t.Fatalf("evaluation = %+v", result)
	}
	if len(result.MissingRanks) != 1 || result.MissingRanks[0] != 1 {
		t.Fatalf("missing ranks = %v", result.MissingRanks)
	}
}

func TestEvaluateRejectsVersionAndLoadAndStepFailures(t *testing.T) {
	versionMismatch := validReport(0)
	versionMismatch.Version = "step-99"
	if got := Evaluate(
		[]RankReport{versionMismatch},
		[]int32{0},
		"step-100",
		10,
	); got.Condition != "VersionMismatch" {
		t.Fatalf("version condition = %+v", got)
	}

	loadFailure := validReport(0)
	loadFailure.OptimizerLoaded = false
	if got := Evaluate(
		[]RankReport{loadFailure},
		[]int32{0},
		"step-100",
		10,
	); got.Condition != "LoadFailed" {
		t.Fatalf("load condition = %+v", got)
	}

	stepFailure := validReport(0)
	stepFailure.StepsRan = 9
	if got := Evaluate(
		[]RankReport{stepFailure},
		[]int32{0},
		"step-100",
		10,
	); got.Condition != "StepFailed" {
		t.Fatalf("step condition = %+v", got)
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

func TestFailureAggregateMarshalsEmptyReportsAsArray(t *testing.T) {
	aggregate := NewAggregate(
		"default/migration",
		"step-100",
		"validate",
		"2026-10-22T00:00:00Z",
		Evaluation{
			Condition:  "JobFailed",
			Message:    "target failed",
			RanksTotal: 2,
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
}

func validReport(rank int32) RankReport {
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
		OK:              true,
	}
}
