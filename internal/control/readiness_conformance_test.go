package control

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// conformanceCase mirrors the shared fixture format consumed by the Python
// conformance test (tests/unit/test_readiness_conformance.py).
type conformanceCase struct {
	Name                 string               `json:"name"`
	Description          string               `json:"description"`
	Now                  string               `json:"now"`
	MaxAgeSeconds        int64                `json:"maxAgeSeconds"`
	Record               CheckpointRecord     `json:"record"`
	Drill                *DrillOutcome        `json:"drill"`
	ResumeState          *StateClosureSummary `json:"resumeState"`
	RequireStateComplete bool                 `json:"requireStateComplete"`
	TargetWorldSize      *int                 `json:"targetWorldSize"`
	Expected             struct {
		Status              string   `json:"status"`
		Complete            bool     `json:"complete"`
		Fresh               bool     `json:"fresh"`
		Loadable            *bool    `json:"loadable"`
		Movable             *bool    `json:"movable"`
		StateComplete       *bool    `json:"stateComplete"`
		ReadinessLevel      string   `json:"readinessLevel"`
		Reasons             []string `json:"reasons"`
		ObservedAt          string   `json:"observedAt"`
		EstimatedRTOSeconds *int64   `json:"estimatedRtoSeconds"`
		TargetWorldSize     *int     `json:"targetWorldSize"`
		LastDrill           *string  `json:"lastDrill"`
		LastDrillDigest     *string  `json:"lastDrillDigest"`
	} `json:"expected"`
}

func loadConformanceCases(t *testing.T) []conformanceCase {
	t.Helper()
	dir := filepath.Join("..", "..", "tests", "fixtures", "readiness", "conformance")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read conformance fixtures: %v", err)
	}
	cases := make([]conformanceCase, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, entry.Name()))
		if err != nil {
			t.Fatalf("read %s: %v", entry.Name(), err)
		}
		var item conformanceCase
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatalf("unmarshal %s: %v", entry.Name(), err)
		}
		if item.Name == "" {
			item.Name = entry.Name()
		}
		cases = append(cases, item)
	}
	if len(cases) < 14 {
		t.Fatalf("expected at least 14 conformance fixtures, got %d", len(cases))
	}
	return cases
}

func boolPtrEqual(actual *bool, expected *bool) bool {
	if actual == nil || expected == nil {
		return actual == nil && expected == nil
	}
	return *actual == *expected
}

func intPtrEqual(actual *int, expected *int) bool {
	if actual == nil || expected == nil {
		return actual == nil && expected == nil
	}
	return *actual == *expected
}

func int64PtrEqual(actual *int64, expected *int64) bool {
	if actual == nil || expected == nil {
		return actual == nil && expected == nil
	}
	return *actual == *expected
}

// stringValueEqual compares an optional string field across the language
// boundary: the Go side uses the zero value where Python uses null.
func stringValueEqual(actual string, expected *string) bool {
	if expected == nil {
		return actual == ""
	}
	return actual == *expected
}

func TestReadinessConformance(t *testing.T) {
	for _, item := range loadConformanceCases(t) {
		item := item
		t.Run(item.Name, func(t *testing.T) {
			now, err := time.Parse(time.RFC3339, item.Now)
			if err != nil {
				t.Fatalf("parse now: %v", err)
			}
			result := AssessReadiness(ReadinessInput{
				Record:               item.Record,
				Now:                  now,
				MaxAge:               time.Duration(item.MaxAgeSeconds) * time.Second,
				TargetWorldSize:      item.TargetWorldSize,
				Drill:                item.Drill,
				State:                item.ResumeState,
				RequireStateComplete: item.RequireStateComplete,
			})
			if string(result.Status) != item.Expected.Status {
				t.Fatalf("status = %s, want %s (%s)", result.Status, item.Expected.Status, item.Description)
			}
			if result.Complete != item.Expected.Complete {
				t.Fatalf("complete = %v, want %v", result.Complete, item.Expected.Complete)
			}
			if result.Fresh != item.Expected.Fresh {
				t.Fatalf("fresh = %v, want %v", result.Fresh, item.Expected.Fresh)
			}
			if !boolPtrEqual(result.Loadable, item.Expected.Loadable) {
				t.Fatalf("loadable mismatch")
			}
			if !boolPtrEqual(result.Movable, item.Expected.Movable) {
				t.Fatalf("movable mismatch")
			}
			if !boolPtrEqual(result.StateComplete, item.Expected.StateComplete) {
				t.Fatalf("state_complete mismatch")
			}
			if result.ReadinessLevel != item.Expected.ReadinessLevel {
				t.Fatalf("readiness_level = %s, want %s", result.ReadinessLevel, item.Expected.ReadinessLevel)
			}
			codes := make([]string, 0, len(result.Reasons))
			for _, reason := range result.Reasons {
				codes = append(codes, reason.Code)
			}
			if len(codes) != len(item.Expected.Reasons) {
				t.Fatalf("reason codes = %v, want %v", codes, item.Expected.Reasons)
			}
			for index, code := range codes {
				if code != item.Expected.Reasons[index] {
					t.Fatalf("reason codes = %v, want %v", codes, item.Expected.Reasons)
				}
			}
			if result.ObservedAt != item.Expected.ObservedAt {
				t.Fatalf("observedAt = %q, want %q", result.ObservedAt, item.Expected.ObservedAt)
			}
			if !int64PtrEqual(result.EstimatedRTOSeconds, item.Expected.EstimatedRTOSeconds) {
				t.Fatalf("estimated_rto_seconds mismatch")
			}
			if !intPtrEqual(result.TargetWorldSize, item.Expected.TargetWorldSize) {
				t.Fatalf("target_world_size mismatch")
			}
			if !stringValueEqual(result.LastDrill, item.Expected.LastDrill) {
				t.Fatalf("last_drill = %q, want %v", result.LastDrill, item.Expected.LastDrill)
			}
			if !stringValueEqual(result.LastDrillDigest, item.Expected.LastDrillDigest) {
				t.Fatalf("last_drill_digest = %q, want %v", result.LastDrillDigest, item.Expected.LastDrillDigest)
			}
		})
	}
}

func TestReadinessObservedAtIsUTC(t *testing.T) {
	for _, item := range loadConformanceCases(t) {
		item := item
		t.Run(item.Name, func(t *testing.T) {
			now, err := time.Parse(time.RFC3339, item.Now)
			if err != nil {
				t.Fatalf("parse now: %v", err)
			}
			result := AssessReadiness(ReadinessInput{
				Record:          item.Record,
				Now:             now,
				MaxAge:          time.Duration(item.MaxAgeSeconds) * time.Second,
				TargetWorldSize: item.TargetWorldSize,
				Drill:           item.Drill,
			})
			if result.ObservedAt != item.Expected.ObservedAt {
				t.Fatalf(
					"observedAt = %q, want the canonical %q",
					result.ObservedAt,
					item.Expected.ObservedAt,
				)
			}
			if !strings.HasSuffix(result.ObservedAt, "Z") {
				t.Fatalf("observedAt = %q, want a Z suffix", result.ObservedAt)
			}
			parsed, err := time.Parse(time.RFC3339, result.ObservedAt)
			if err != nil {
				t.Fatalf("observedAt is not RFC3339: %v", err)
			}
			_, offset := parsed.Zone()
			if offset != 0 {
				t.Fatalf("observedAt offset = %d, want 0", offset)
			}
		})
	}
}
