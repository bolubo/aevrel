package controller

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

var migrationPhaseTransitions = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Namespace: "aevrel",
		Subsystem: "controller",
		Name:      "migration_phase_transitions_total",
		Help:      "Number of observed CheckpointMigration phase transitions.",
	},
	[]string{"from", "to"},
)

func init() {
	metrics.Registry.MustRegister(migrationPhaseTransitions)
}

func observeMigrationPhaseTransition(from, to string) {
	if from == "" || from == to {
		return
	}
	migrationPhaseTransitions.WithLabelValues(from, to).Inc()
}
