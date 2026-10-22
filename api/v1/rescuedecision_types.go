package v1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

type RescueDecisionSpec struct {
	// +kubebuilder:validation:MinLength=1
	Event         string `json:"event"`
	WorkloadRef   string `json:"workloadRef,omitempty"`
	CheckpointRef string `json:"checkpointRef,omitempty"`
	Target        string `json:"target,omitempty"`
	// Fallback may be hold or restart. hold is the default and is the only
	// safe choice when source termination is not independently proven.
	// +kubebuilder:validation:Enum=hold;restart
	// +kubebuilder:default=hold
	Fallback string `json:"fallback,omitempty"`
}

type RescueDecisionStatus struct {
	Action     string             `json:"action,omitempty"`
	ReasonCode string             `json:"reasonCode,omitempty"`
	Reason     string             `json:"reason,omitempty"`
	Target     string             `json:"target,omitempty"`
	ProofRef   string             `json:"proofRef,omitempty"`
	DecidedAt  string             `json:"decidedAt,omitempty"`
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=sfrescue
type RescueDecision struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              RescueDecisionSpec   `json:"spec,omitempty"`
	Status            RescueDecisionStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type RescueDecisionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RescueDecision `json:"items"`
}
