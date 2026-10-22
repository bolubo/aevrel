package execution

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var rankPhaseTransitions = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Namespace: "shardferry",
		Subsystem: "execution",
		Name:      "rank_phase_transitions_total",
		Help:      "Number of observed target rank phase transitions.",
	},
	[]string{"from", "to"},
)

func init() {
	metrics.Registry.MustRegister(rankPhaseTransitions)
}

func observeRankPhaseTransition(from, to Phase) {
	if from == to {
		return
	}
	rankPhaseTransitions.WithLabelValues(string(from), string(to)).Inc()
}
