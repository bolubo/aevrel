package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

const (
	MigrationPhasePending          = "pending"
	MigrationPhasePreparing        = "preparing"
	MigrationPhaseTransferring     = "transferring"
	MigrationPhaseCommitPending    = "commit-pending"
	MigrationPhaseCommitting       = "committing"
	MigrationPhaseSucceeded        = "succeeded"
	MigrationPhaseFailed           = "failed"
	MigrationPhaseCancelled        = "cancelled"
	MigrationPhaseCommitIncomplete = "commit-incomplete"
)

// CheckpointSpec describes the supported checkpoint format.
type CheckpointSpec struct {
	// +kubebuilder:validation:Enum=dcp
	Format string `json:"format"`
	// +kubebuilder:validation:MinLength=1
	FormatVersion string `json:"formatVersion"`
}

// MeshSpec describes a DeviceMesh.
type MeshSpec struct {
	// +kubebuilder:validation:MinItems=1
	Shape []int32 `json:"shape"`
	// +kubebuilder:validation:MinItems=1
	Placements []string `json:"placements"`
}

// RankEndpoint maps one rank to an agent endpoint.
type RankEndpoint struct {
	// +kubebuilder:validation:Minimum=0
	Rank int32 `json:"rank"`
	// +kubebuilder:validation:MinLength=1
	Endpoint string `json:"endpoint"`
}

// AgentSelector discovers agent pods for a migration.
type AgentSelector struct {
	// Namespace defaults to the CheckpointMigration namespace when empty.
	Namespace string `json:"namespace,omitempty"`
	// MatchLabels selects agent pods by their labels.
	MatchLabels map[string]string `json:"matchLabels,omitempty"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port,omitempty"`
	// +kubebuilder:validation:Enum=http;https
	Scheme string `json:"scheme,omitempty"`
}

// ClusterSecretRef points at the bearer token used to call a cluster gateway.
type ClusterSecretRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// Key defaults to token.
	Key string `json:"key,omitempty"`
}

// ShardferryClusterSpec describes a reachable agent gateway in one cluster.
type ShardferryClusterSpec struct {
	// +kubebuilder:validation:Enum=source;target;both
	// +kubebuilder:default=both
	Role string `json:"role,omitempty"`
	// +kubebuilder:validation:MinLength=1
	Endpoint    string           `json:"endpoint"`
	SecretRef   ClusterSecretRef `json:"secretRef"`
	Description string           `json:"description,omitempty"`
}

// ShardferryClusterStatus is the observed gateway state.
type ShardferryClusterStatus struct {
	Ready    bool   `json:"ready,omitempty"`
	Version  string `json:"version,omitempty"`
	Message  string `json:"message,omitempty"`
	LastSeen string `json:"lastSeen,omitempty"`
}

// SourceSpec describes the source checkpoint location and topology.
type SourceSpec struct {
	// +kubebuilder:validation:Minimum=0
	MetadataOwnerRank int32 `json:"metadataOwnerRank"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=256
	WorldSize     int32          `json:"worldSize"`
	Mesh          MeshSpec       `json:"mesh"`
	ClusterRef    string         `json:"clusterRef,omitempty"`
	RankMap       []RankEndpoint `json:"rankMap,omitempty"`
	AgentSelector *AgentSelector `json:"agentSelector,omitempty"`
	// +kubebuilder:validation:MinLength=1
	CheckpointRoot string `json:"checkpointRoot"`
}

// TargetSpec describes the target topology and local roots.
type TargetSpec struct {
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=256
	WorldSize     int32          `json:"worldSize"`
	Mesh          MeshSpec       `json:"mesh"`
	ClusterRef    string         `json:"clusterRef,omitempty"`
	RankMap       []RankEndpoint `json:"rankMap,omitempty"`
	AgentSelector *AgentSelector `json:"agentSelector,omitempty"`
	// +kubebuilder:validation:MinLength=1
	RootTemplate string `json:"rootTemplate"`
}

// PolicySpec describes migration safety and concurrency policy.
type PolicySpec struct {
	// +kubebuilder:validation:Enum=bitwise
	Verify string `json:"verify"`
	// +kubebuilder:validation:Enum=all-or-nothing
	Commit string `json:"commit"`
	// +kubebuilder:validation:Minimum=1
	MaxConcurrentTransfers int32 `json:"maxConcurrentTransfers"`
	// +kubebuilder:validation:Minimum=1
	MaxAttemptsPerRank int32 `json:"maxAttemptsPerRank"`
	// +kubebuilder:validation:Minimum=1
	TimeoutSeconds int64 `json:"timeoutSeconds"`
	// +kubebuilder:validation:Minimum=1
	SourceChunkCacheBytes int64 `json:"sourceChunkCacheBytes"`
}

// ArtifactSpec describes where plan/report artifacts live.
type ArtifactSpec struct {
	// +kubebuilder:validation:Enum=pvc;s3
	Store     string `json:"store"`
	ClaimName string `json:"claimName,omitempty"`
	Bucket    string `json:"bucket,omitempty"`
	Prefix    string `json:"prefix,omitempty"`
}

// CheckpointMigrationSpec defines the desired migration.
type CheckpointMigrationSpec struct {
	Checkpoint CheckpointSpec `json:"checkpoint"`
	Source     SourceSpec     `json:"source"`
	Target     TargetSpec     `json:"target"`
	Policy     PolicySpec     `json:"policy"`
	Artifacts  ArtifactSpec   `json:"artifacts"`
}

// RankCounts is a summary of rank states.
type RankCounts struct {
	Prepared  int32 `json:"prepared"`
	Staged    int32 `json:"staged"`
	Committed int32 `json:"committed"`
	Failed    int32 `json:"failed"`
}

// CheckpointMigrationStatus is the observed migration state.
type CheckpointMigrationStatus struct {
	// +kubebuilder:validation:Enum=pending;preparing;transferring;commit-pending;committing;succeeded;failed;cancelled;commit-incomplete
	Phase    string `json:"phase,omitempty"`
	PlanRef  string `json:"planRef,omitempty"`
	PlanHash string `json:"planHash,omitempty"`
	// +kubebuilder:validation:Enum=following;catching-up;target-verified;promoting;promoted;commit-incomplete;failed;cancelled
	CutoverPhase string             `json:"cutoverPhase,omitempty"`
	ExecutionRef string             `json:"executionRef,omitempty"`
	ReportRef    string             `json:"reportRef,omitempty"`
	ProofRef     string             `json:"proofRef,omitempty"`
	ProofHash    string             `json:"proofHash,omitempty"`
	CommitToken  string             `json:"commitToken,omitempty"`
	Counts       RankCounts         `json:"counts,omitempty"`
	FailedRanks  []int32            `json:"failedRanks,omitempty"`
	StartedAt    string             `json:"startedAt,omitempty"`
	CompletedAt  string             `json:"completedAt,omitempty"`
	Conditions   []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
type CheckpointMigration struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              CheckpointMigrationSpec   `json:"spec,omitempty"`
	Status            CheckpointMigrationStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type CheckpointMigrationList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []CheckpointMigration `json:"items"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=sfc
type ShardferryCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ShardferryClusterSpec   `json:"spec,omitempty"`
	Status            ShardferryClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ShardferryClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ShardferryCluster `json:"items"`
}

func init() {
	SchemeBuilder.Register(
		&CheckpointMigration{},
		&CheckpointMigrationList{},
		&ShardferryCluster{},
		&ShardferryClusterList{},
	)
}

var _ runtime.Object = &CheckpointMigration{}
