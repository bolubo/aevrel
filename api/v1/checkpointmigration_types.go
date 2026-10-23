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
	MigrationPhaseTargetValidating = "target-validating"
	MigrationPhaseTargetFailed     = "target-failed"
	MigrationPhaseSucceeded        = "succeeded"
	MigrationPhaseFailed           = "failed"
	MigrationPhaseCancelled        = "cancelled"
	MigrationPhaseCommitIncomplete = "commit-incomplete"

	MigrationModeStatic        = "static"
	MigrationModeLiveRankSplit = "live-rank-split"
	LiveStateModeL3            = "L3"
	LiveStateModeL4            = "L4"
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

// TargetValidationSpec describes the target training workload used to prove
// that the committed checkpoint can actually be loaded and resumed.
type TargetValidationSpec struct {
	// Version is the checkpoint version/step the target must load.
	// +kubebuilder:validation:MinLength=1
	Version string `json:"version"`
	// Steps is the minimum number of training steps every target rank must run.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=10
	Steps int32 `json:"steps,omitempty"`
	// TimeoutSeconds bounds the validation Job lifetime.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=600
	TimeoutSeconds int64 `json:"timeoutSeconds,omitempty"`
	// JobName observes an existing Job instead of creating one from JobTemplate.
	JobName string `json:"jobName,omitempty"`
	// JobTemplate is a batch/v1 Job with one Indexed Pod per target rank.
	// aevrel injects the checkpoint and report contract into every
	// container before creating it.
	// +kubebuilder:pruning:PreserveUnknownFields
	JobTemplate *runtime.RawExtension `json:"jobTemplate,omitempty"`
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

// AevrelClusterSpec describes a reachable agent gateway in one cluster.
type AevrelClusterSpec struct {
	// +kubebuilder:validation:Enum=source;target;both
	// +kubebuilder:default=both
	Role string `json:"role,omitempty"`
	// +kubebuilder:validation:MinLength=1
	Endpoint    string           `json:"endpoint"`
	SecretRef   ClusterSecretRef `json:"secretRef"`
	Description string           `json:"description,omitempty"`
}

// AevrelClusterStatus is the observed gateway state.
type AevrelClusterStatus struct {
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
	RootTemplate string                `json:"rootTemplate"`
	Validation   *TargetValidationSpec `json:"validation,omitempty"`
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

// LiveRankPlanSpec describes the TP/PP layout and the logical ranks that may
// be in flight during a live rank split.
type LiveRankPlanSpec struct {
	// +kubebuilder:validation:Minimum=0
	TP int32 `json:"tp,omitempty"`
	// +kubebuilder:validation:Minimum=0
	PP int32 `json:"pp,omitempty"`
	// Ranks are logical source ranks that may be in flight.
	Ranks []int32 `json:"ranks,omitempty"`
}

// LiveCutoverSpec describes the quorum/fencing contract at cutover.
type LiveCutoverSpec struct {
	// Quorum defaults to floor(source.worldSize/2)+1.
	// +kubebuilder:validation:Minimum=0
	Quorum int32 `json:"quorum,omitempty"`
	// +kubebuilder:default=true
	RequireStateComplete *bool `json:"requireStateComplete,omitempty"`
}

// LiveRankSplitSpec configures a live rank split. The source and target
// clusters share a logical world size; ranks move while the remaining ranks
// continue the same synchronous step.
type LiveRankSplitSpec struct {
	// MaxInFlight is F, the maximum number of concurrently moving ranks.
	// +kubebuilder:validation:Minimum=1
	MaxInFlight int32 `json:"maxInFlight"`
	// +kubebuilder:validation:Enum=L3;L4
	// +kubebuilder:default=L4
	StateMode string `json:"stateMode,omitempty"`
	// StateRoot is the rank-local state bundle root on the source.
	// +kubebuilder:validation:MinLength=1
	StateRoot string `json:"stateRoot"`
	// StateProviders is the explicit provider list for the bundle.
	StateProviders []string `json:"stateProviders,omitempty"`
	// HandoffEndpoint is the live state handoff service.
	HandoffEndpoint string            `json:"handoffEndpoint,omitempty"`
	Cutover         LiveCutoverSpec   `json:"cutover,omitempty"`
	RankPlan        *LiveRankPlanSpec `json:"rankPlan,omitempty"`
}

// CheckpointMigrationSpec defines the desired migration.
type CheckpointMigrationSpec struct {
	// +kubebuilder:validation:Enum=static;live-rank-split
	// +kubebuilder:default=static
	Mode       string             `json:"mode,omitempty"`
	Live       *LiveRankSplitSpec `json:"live,omitempty"`
	Checkpoint CheckpointSpec     `json:"checkpoint"`
	Source     SourceSpec         `json:"source"`
	Target     TargetSpec         `json:"target"`
	Policy     PolicySpec         `json:"policy"`
	Artifacts  ArtifactSpec       `json:"artifacts"`
}

// RankCounts is a summary of rank states.
type RankCounts struct {
	Prepared  int32 `json:"prepared"`
	Staged    int32 `json:"staged"`
	Committed int32 `json:"committed"`
	Failed    int32 `json:"failed"`
}

// RankStatus is the observed state of one target rank.
type RankStatus struct {
	Rank     int32  `json:"rank"`
	Phase    string `json:"phase"`
	TaskID   string `json:"taskID,omitempty"`
	Attempts int32  `json:"attempts,omitempty"`
	Error    string `json:"error,omitempty"`
}

// TargetReadyRankStatus is the observed target validation state of one rank.
type TargetReadyRankStatus struct {
	Rank            int32  `json:"rank"`
	ModelLoaded     bool   `json:"modelLoaded"`
	OptimizerLoaded bool   `json:"optimizerLoaded"`
	StepsRan        int32  `json:"stepsRan"`
	StateComplete   bool   `json:"stateComplete"`
	OK              bool   `json:"ok"`
	Error           string `json:"error,omitempty"`
}

// TargetReadyStatus proves whether the target workload loaded the committed
// checkpoint and continued training for the required number of steps.
type TargetReadyStatus struct {
	// +kubebuilder:validation:Enum=Pending;Ready;LoadFailed;StepFailed;VersionMismatch;Timeout;JobFailed;Incomplete;InvalidJob;InvalidReport;Unsupported;ProofPublishFailed
	Condition            string                  `json:"condition,omitempty"`
	Ready                bool                    `json:"ready"`
	Version              string                  `json:"version,omitempty"`
	RanksReady           int32                   `json:"ranksReady"`
	RanksTotal           int32                   `json:"ranksTotal"`
	StepsRan             int32                   `json:"stepsRan"`
	StateComplete        bool                    `json:"stateComplete"`
	DurationMilliseconds int64                   `json:"durationMilliseconds,omitempty"`
	JobName              string                  `json:"jobName,omitempty"`
	ProofRef             string                  `json:"proofRef,omitempty"`
	ProofHash            string                  `json:"proofHash,omitempty"`
	Message              string                  `json:"message,omitempty"`
	StartedAt            string                  `json:"startedAt,omitempty"`
	CompletedAt          string                  `json:"completedAt,omitempty"`
	Ranks                []TargetReadyRankStatus `json:"ranks,omitempty"`
}

// LiveRankStatus is the observed state of one logical rank in a live split.
type LiveRankStatus struct {
	Rank    int32  `json:"rank"`
	Phase   string `json:"phase"`
	Owner   int32  `json:"owner,omitempty"`
	Epoch   int64  `json:"epoch,omitempty"`
	Retries int32  `json:"retries,omitempty"`
	Error   string `json:"error,omitempty"`
}

// LiveRankSplitStatus is the observed live rank split state.
type LiveRankSplitStatus struct {
	// +kubebuilder:validation:Enum=pending;preflight;draining;snapshotting;following;cutover;target-validating;succeeded;failed;cancelled
	CutoverPhase  string           `json:"cutoverPhase,omitempty"`
	InFlight      []int32          `json:"inFlight,omitempty"`
	Coverage      []int32          `json:"coverage,omitempty"`
	Quorum        int32            `json:"quorum,omitempty"`
	StateComplete bool             `json:"stateComplete,omitempty"`
	StateRoot     string           `json:"stateRoot,omitempty"`
	ProofRef      string           `json:"proofRef,omitempty"`
	Message       string           `json:"message,omitempty"`
	CompletedAt   string           `json:"completedAt,omitempty"`
	Ranks         []LiveRankStatus `json:"ranks,omitempty"`
}

// CheckpointMigrationStatus is the observed migration state.
type CheckpointMigrationStatus struct {
	// +kubebuilder:validation:Enum=pending;preparing;transferring;commit-pending;committing;target-validating;target-failed;succeeded;failed;cancelled;commit-incomplete
	Phase    string `json:"phase,omitempty"`
	PlanRef  string `json:"planRef,omitempty"`
	PlanHash string `json:"planHash,omitempty"`
	// +kubebuilder:validation:Enum=following;catching-up;target-verified;promoting;promoted;commit-incomplete;failed;cancelled
	CutoverPhase string               `json:"cutoverPhase,omitempty"`
	ExecutionRef string               `json:"executionRef,omitempty"`
	ReportRef    string               `json:"reportRef,omitempty"`
	ProofRef     string               `json:"proofRef,omitempty"`
	ProofHash    string               `json:"proofHash,omitempty"`
	CommitToken  string               `json:"commitToken,omitempty"`
	Counts       RankCounts           `json:"counts,omitempty"`
	Ranks        []RankStatus         `json:"ranks,omitempty"`
	FailedRanks  []int32              `json:"failedRanks,omitempty"`
	StartedAt    string               `json:"startedAt,omitempty"`
	CompletedAt  string               `json:"completedAt,omitempty"`
	TargetReady  *TargetReadyStatus   `json:"targetReady,omitempty"`
	Live         *LiveRankSplitStatus `json:"live,omitempty"`
	Conditions   []metav1.Condition   `json:"conditions,omitempty"`
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
type AevrelCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              AevrelClusterSpec   `json:"spec,omitempty"`
	Status            AevrelClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type AevrelClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AevrelCluster `json:"items"`
}

func init() {
	SchemeBuilder.Register(
		&CheckpointMigration{},
		&CheckpointMigrationList{},
		&AevrelCluster{},
		&AevrelClusterList{},
	)
}

var _ runtime.Object = &CheckpointMigration{}
