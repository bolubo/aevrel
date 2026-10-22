package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

type RestoreDrillSpec struct {
	// +kubebuilder:validation:MinLength=1
	RecordRef string `json:"recordRef"`
	// JobName observes an existing Job instead of creating one from JobTemplate.
	JobName string `json:"jobName,omitempty"`
	// JobTemplate is a batch/v1 Job with one Indexed Pod per target rank.
	// shardferry injects the restore/report contract before creating it.
	// +kubebuilder:pruning:PreserveUnknownFields
	JobTemplate *runtime.RawExtension `json:"jobTemplate,omitempty"`
	// +kubebuilder:validation:MinLength=1
	Version string `json:"version"`
	// +kubebuilder:validation:Minimum=1
	TargetWorldSize int32 `json:"targetWorldSize"`
	// +kubebuilder:validation:Minimum=1
	Steps int32 `json:"steps"`
	// +kubebuilder:validation:Minimum=1
	TimeoutSeconds int64 `json:"timeoutSeconds,omitempty"`
	// CheckpointTemplate is the target-side path passed to each rank. It
	// defaults to the CheckpointRecord path for same-cluster drills.
	CheckpointTemplate   string   `json:"checkpointTemplate,omitempty"`
	StateTemplate        string   `json:"stateTemplate,omitempty"`
	StateProviders       []string `json:"stateProviders,omitempty"`
	RequireStateComplete bool     `json:"requireStateComplete,omitempty"`
}

type RestoreDrillStatus struct {
	Phase           string             `json:"phase,omitempty"`
	ModelLoaded     bool               `json:"modelLoaded,omitempty"`
	OptimizerLoaded bool               `json:"optimizerLoaded,omitempty"`
	StateComplete   bool               `json:"stateComplete,omitempty"`
	StepsRan        int32              `json:"stepsRan,omitempty"`
	RanksReady      int32              `json:"ranksReady,omitempty"`
	RanksTotal      int32              `json:"ranksTotal,omitempty"`
	MissingRanks    []int32            `json:"missingRanks,omitempty"`
	JobName         string             `json:"jobName,omitempty"`
	StartedAt       string             `json:"startedAt,omitempty"`
	CompletedAt     string             `json:"completedAt,omitempty"`
	ProofRef        string             `json:"proofRef,omitempty"`
	Message         string             `json:"message,omitempty"`
	Conditions      []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=sfdrill
type RestoreDrill struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              RestoreDrillSpec   `json:"spec,omitempty"`
	Status            RestoreDrillStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type RestoreDrillList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RestoreDrill `json:"items"`
}
