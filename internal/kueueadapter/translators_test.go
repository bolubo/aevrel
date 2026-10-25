package kueueadapter

import (
	"testing"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

func TestAvailableTranslatorsCoversEveryDocumentedConsumer(t *testing.T) {
	want := []struct {
		name      string
		supported bool
	}{
		{"generic", true},
		{"kueue", true},
		{"volcano", false},
		{"kubeflow", false},
	}
	translators := AvailableTranslators()
	if len(translators) != len(want) {
		t.Fatalf("got %d translators, want %d", len(translators), len(want))
	}
	for index, expectation := range want {
		if translators[index].Name() != expectation.name {
			t.Fatalf(
				"translator[%d] = %q, want %q",
				index,
				translators[index].Name(),
				expectation.name,
			)
		}
		view := translators[index].Translate(validRecordFacts())
		if view.Supported != expectation.supported {
			t.Fatalf(
				"%s: supported = %t, want %t",
				expectation.name,
				view.Supported,
				expectation.supported,
			)
		}
	}
	if _, found := TranslatorFor("slurm"); found {
		t.Fatal("TranslatorFor must not invent a translator")
	}
}

func TestKueueTranslatorMapsVerifiedFacts(t *testing.T) {
	view := KueueTranslator{}.Translate(validRecordFacts())
	if !view.Supported {
		t.Fatal("the Kueue translator is implemented")
	}
	assertField(t, view, "numericLabels", LabelEnvelopeComplete+"=1,"+
		LabelEnvelopeFresh+"=1,"+LabelEnvelopeLoadable+"=1")
	assertField(t, view, "preemptionCost", "resume")
	assertField(t, view, "estimatedRecoverySeconds", "120")
}

func TestKueueTranslatorOmitsUnknownNumericLabels(t *testing.T) {
	// A missing value must never become "=0": zero is a measured answer.
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:     aevrelv1.ReadinessUnverified,
		Complete:   true,
		Fresh:      false,
		ObservedAt: factTestObservedAt,
	}
	view := KueueTranslator{}.Translate(
		CheckpointFactsForRecord(record, 1, record.Name, FactsOptions{}),
	)
	assertField(t, view, "numericLabels", LabelEnvelopeComplete+"=1,"+
		LabelEnvelopeFresh+"=0")
}

func TestTranslatorsNeverUpgradeAnUnknownEnvelope(t *testing.T) {
	facts := CheckpointFactsForNoSignal(FactsOptions{})
	generic := GenericTranslator{}.Translate(facts)
	assertField(t, generic, "checkpointResumable", FactValueUnknown)
	assertField(t, generic, "preemptionCost", FactValueUnknown)
	assertField(t, generic, "estimatedRecoverySeconds", FactValueUnknown)

	kueue := KueueTranslator{}.Translate(facts)
	assertField(t, kueue, "numericLabels", "")
	assertField(t, kueue, "preemptionCost", FactValueUnknown)
}

func TestRestartCostIsNotReportedAsResume(t *testing.T) {
	facts := CheckpointFactsForNoSignal(FactsOptions{})
	facts.Conformance = ConformanceReport{Valid: true}
	facts.RestoreMode = string(control.RescueRestart)
	if got := preemptionCostValue(facts); got != "restart" {
		t.Fatalf("preemptionCost = %q", got)
	}
}

func TestVolcanoAndKubeflowMakeNoClaims(t *testing.T) {
	for _, translator := range []Translator{
		VolcanoTranslator{},
		KubeflowTranslator{},
	} {
		view := translator.Translate(validRecordFacts())
		if view.Supported {
			t.Fatalf("%s claims support it does not have", translator.Name())
		}
		if len(view.Fields) != 0 {
			t.Fatalf(
				"%s published fields without an implementation: %v",
				translator.Name(),
				view.Fields,
			)
		}
		if len(view.Notes) == 0 {
			t.Fatalf("%s must explain why it is unsupported", translator.Name())
		}
	}
}

func assertField(t *testing.T, view ConsumerView, name, want string) {
	t.Helper()
	for _, field := range view.Fields {
		if field.Name == name {
			if field.Value != want {
				t.Fatalf(
					"%s: %s = %q, want %q",
					view.Consumer,
					name,
					field.Value,
					want,
				)
			}
			return
		}
	}
	t.Fatalf("%s: missing field %s", view.Consumer, name)
}
