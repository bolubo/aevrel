package kueueadapter

import (
	"strconv"
	"strings"

	"github.com/bolubo/aevrel/internal/control"
)

// ConsumerField is one named value in a consumer's vocabulary. Fields are
// emitted in a fixed order so a caller can diff two views.
type ConsumerField struct {
	Name  string
	Value string
}

// ConsumerView is one consumer's reading of the envelope.
//
// Supported=false means exactly that: this adapter does not consume the
// contract yet, and the view carries no claims. It is never a soft "yes".
type ConsumerView struct {
	Consumer  string
	Supported bool
	Fields    []ConsumerField
	Notes     []string
}

// Translator restates aevrel facts in one consumer's vocabulary. It translates;
// it never decides. Fairness, quotas, priority classes, and preemption formulas
// stay with the scheduler.
type Translator interface {
	Name() string
	Translate(facts CheckpointFacts) ConsumerView
}

// AvailableTranslators lists every translator this build ships. Only Kueue is
// implemented; Volcano and Kubeflow are reserved interfaces that report
// themselves as unsupported.
func AvailableTranslators() []Translator {
	return []Translator{
		GenericTranslator{},
		KueueTranslator{},
		VolcanoTranslator{},
		KubeflowTranslator{},
	}
}

// TranslatorFor returns the named translator.
func TranslatorFor(name string) (Translator, bool) {
	for _, translator := range AvailableTranslators() {
		if translator.Name() == name {
			return translator, true
		}
	}
	return nil, false
}

// GenericTranslator is the minimum bar for any scheduler: it turns the envelope
// into three plain answers without assuming a particular project.
type GenericTranslator struct{}

func (GenericTranslator) Name() string { return "generic" }

func (GenericTranslator) Translate(facts CheckpointFacts) ConsumerView {
	view := ConsumerView{
		Consumer:  "generic",
		Supported: true,
		Notes: []string{
			"Facts are advisory. The consumer still owns admission and preemption.",
		},
	}
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "checkpointResumable",
		Value: resumableValue(facts),
	})
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "preemptionCost",
		Value: preemptionCostValue(facts),
	})
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "estimatedRecoverySeconds",
		Value: rtoValue(facts.EstimatedRTOSeconds),
	})
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "evidenceSource",
		Value: facts.Source,
	})
	return view
}

// KueueTranslator maps the envelope onto what Kueue can actually select on.
//
// Kueue reads integer numeric labels, so the translator exposes those labels
// and nothing else. It deliberately does not produce a priority class or a
// preemption-cost formula: those are Kueue-side policy.
type KueueTranslator struct{}

func (KueueTranslator) Name() string { return "kueue" }

func (KueueTranslator) Translate(facts CheckpointFacts) ConsumerView {
	view := ConsumerView{Consumer: "kueue", Supported: true}
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "numericLabels",
		Value: numericLabelCandidates(facts),
	})
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "preemptionCost",
		Value: preemptionCostValue(facts),
	})
	view.Fields = append(view.Fields, ConsumerField{
		Name:  "estimatedRecoverySeconds",
		Value: rtoValue(facts.EstimatedRTOSeconds),
	})
	view.Notes = append(
		view.Notes,
		"Admission still follows the binary protection label; the envelope only refines the cost picture.",
	)
	return view
}

// VolcanoTranslator is reserved. Aevrel does not claim Volcano support, so the
// view is explicitly unsupported and empty rather than approximately right.
type VolcanoTranslator struct{}

func (VolcanoTranslator) Name() string { return "volcano" }

func (VolcanoTranslator) Translate(CheckpointFacts) ConsumerView {
	return ConsumerView{
		Consumer:  "volcano",
		Supported: false,
		Notes: []string{
			"Not implemented. Aevrel publishes facts; no Volcano translator consumes them yet.",
		},
	}
}

// KubeflowTranslator is reserved for the same reason as Volcano.
type KubeflowTranslator struct{}

func (KubeflowTranslator) Name() string { return "kubeflow" }

func (KubeflowTranslator) Translate(CheckpointFacts) ConsumerView {
	return ConsumerView{
		Consumer:  "kubeflow",
		Supported: false,
		Notes: []string{
			"Not implemented. Aevrel publishes facts; no Kubeflow translator consumes them yet.",
		},
	}
}

// resumableValue answers one question only: may a consumer treat this
// checkpoint as resumable? Unknown never becomes "yes".
func resumableValue(facts CheckpointFacts) string {
	if !facts.Conformance.Valid {
		return FactValueUnknown
	}
	if facts.Complete == nil || facts.Fresh == nil || facts.Loadable == nil {
		return FactValueUnknown
	}
	if *facts.Complete && *facts.Fresh && *facts.Loadable {
		return "yes"
	}
	return "no"
}

// preemptionCostValue restates the advisory restore mode. It is a category, not
// a number: aevrel does not own the preemption cost formula.
func preemptionCostValue(facts CheckpointFacts) string {
	if !facts.Conformance.Valid {
		return FactValueUnknown
	}
	switch facts.RestoreMode {
	case string(control.RescueRestore), string(control.RescueMigrate):
		return "resume"
	case string(control.RescueRestart):
		return "restart"
	default:
		return FactValueUnknown
	}
}

func rtoValue(value *int64) string {
	if value == nil {
		return FactValueUnknown
	}
	return strconv.FormatInt(*value, 10)
}

// numericLabelCandidates lists the envelope facts Kueue can select on. Unknown
// values are omitted instead of being written as 0, because a numeric label of
// 0 would read as a measured zero.
func numericLabelCandidates(facts CheckpointFacts) string {
	pairs := make([]string, 0, 4)
	for _, entry := range []struct {
		key   string
		value *bool
	}{
		{LabelEnvelopeComplete, facts.Complete},
		{LabelEnvelopeFresh, facts.Fresh},
		{LabelEnvelopeLoadable, facts.Loadable},
		{LabelEnvelopeMovable, facts.Movable},
	} {
		if entry.value == nil {
			continue
		}
		pairs = append(pairs, entry.key+"="+boolLabel(*entry.value))
	}
	return strings.Join(pairs, ",")
}
