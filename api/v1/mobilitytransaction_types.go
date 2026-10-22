package v1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

const (
	TransactionModeStatic = "static"
	TransactionModeLive   = "live"

	TransactionPending          = "Pending"
	TransactionPreparing        = "Preparing"
	TransactionTransferring     = "Transferring"
	TransactionVerifying        = "Verifying"
	TransactionCommitting       = "Committing"
	TransactionSucceeded        = "Succeeded"
	TransactionFailed           = "Failed"
	TransactionCancelled        = "Cancelled"
	TransactionCommitIncomplete = "CommitIncomplete"
)

type MobilityTransactionSpec struct {
	// +kubebuilder:validation:MinLength=1
	RecordRef string `json:"recordRef"`
	// MigrationRef links this transaction view to an existing
	// CheckpointMigration. When empty, a migration with the same name is used.
	MigrationRef string `json:"migrationRef,omitempty"`
	// MigrationSpec creates an owned CheckpointMigration that runs through the
	// existing planning/executor path. Mutually exclusive with MigrationRef.
	MigrationSpec *CheckpointMigrationSpec `json:"migrationSpec,omitempty"`
	// +kubebuilder:validation:Enum=static;live
	Mode   string `json:"mode"`
	Source string `json:"source"`
	Target string `json:"target"`
}

type MobilityTransactionStatus struct {
	Phase          string             `json:"phase,omitempty"`
	SourceDigest   string             `json:"sourceDigest,omitempty"`
	TargetDigest   string             `json:"targetDigest,omitempty"`
	MigrationPhase string             `json:"migrationPhase,omitempty"`
	ProofRef       string             `json:"proofRef,omitempty"`
	Message        string             `json:"message,omitempty"`
	TargetReady    *TargetReadyStatus `json:"targetReady,omitempty"`
	Conditions     []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=sfmove
type MobilityTransaction struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              MobilityTransactionSpec   `json:"spec,omitempty"`
	Status            MobilityTransactionStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type MobilityTransactionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []MobilityTransaction `json:"items"`
}
