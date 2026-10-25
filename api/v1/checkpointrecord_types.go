package v1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

const (
	ReadinessUnknown       = "UNKNOWN"
	ReadinessIncomplete    = "INCOMPLETE"
	ReadinessStale         = "STALE"
	ReadinessUnverified    = "UNVERIFIED"
	ReadinessReady         = "READY"
	ReadinessTargetBlocked = "TARGET_BLOCKED"
)

type ControlReason struct {
	// +kubebuilder:validation:MinLength=1
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

type CheckpointShardStatus struct {
	Name         string `json:"name"`
	RelativePath string `json:"relativePath"`
	Rank         int32  `json:"rank"`
	Offset       int64  `json:"offset"`
	Length       int64  `json:"length"`
	SizeBytes    int64  `json:"sizeBytes"`
}

type CheckpointRankStatus struct {
	Rank   int32                   `json:"rank"`
	Shards []CheckpointShardStatus `json:"shards,omitempty"`
	Bytes  int64                   `json:"bytes"`
}

type CheckpointReadinessStatus struct {
	Status              string          `json:"status"`
	Complete            bool            `json:"complete"`
	Fresh               bool            `json:"fresh"`
	Loadable            *bool           `json:"loadable"`
	Movable             *bool           `json:"movable"`
	Reasons             []ControlReason `json:"reasons,omitempty"`
	ObservedAt          string          `json:"observedAt"`
	TargetWorldSize     *int32          `json:"targetWorldSize,omitempty"`
	EstimatedRTOSeconds *int64          `json:"estimatedRTOSeconds,omitempty"`
	LastDrill           string          `json:"lastDrill,omitempty"`
	LastDrillVersion    string          `json:"lastDrillVersion,omitempty"`
	LastDrillDigest     string          `json:"lastDrillDigest,omitempty"`
	// StateComplete reports the resume-state closure as observed by the
	// agents. It is a pointer so "no agent reported closure evidence" stays
	// distinguishable from "the closure is incomplete".
	StateComplete *bool `json:"stateComplete,omitempty"`
	// ReadinessLevel is L3 (bytes verified) or L4 (closure verified and
	// anchored to the manifest a restore drill actually used).
	ReadinessLevel string `json:"readinessLevel,omitempty"`
	// ResumeClosure is the closure verdict the agents reported.
	ResumeClosure string `json:"resumeClosure,omitempty"`
	// ResumeManifestDigest binds that verdict to one manifest.
	ResumeManifestDigest string `json:"resumeManifestDigest,omitempty"`
	// ResumeReasonCodes lists the closure reasons, sorted.
	ResumeReasonCodes []string `json:"resumeReasonCodes,omitempty"`
}

type CheckpointRecordSpec struct {
	// +kubebuilder:validation:MinLength=1
	JobRef string `json:"jobRef"`
	// +kubebuilder:validation:MinLength=1
	Version       string `json:"version"`
	Step          *int64 `json:"step,omitempty"`
	Provider      string `json:"provider"`
	FormatVersion string `json:"formatVersion"`
	// +kubebuilder:validation:MinLength=1
	Checkpoint string `json:"checkpoint"`
	// +kubebuilder:validation:Minimum=1
	SourceWorldSize int32 `json:"sourceWorldSize"`
	// AgentEndpoint observes one explicit agent. Mutually exclusive with
	// AgentSelector.
	AgentEndpoint string `json:"agentEndpoint,omitempty"`
	// AgentSelector discovers every ready agent pod and merges their local
	// checkpoint facts. Mutually exclusive with AgentEndpoint.
	AgentSelector  *AgentSelector    `json:"agentSelector,omitempty"`
	AgentSecretRef *ClusterSecretRef `json:"agentSecretRef,omitempty"`
	// RequireStateComplete turns the readiness assessment into an L4 claim:
	// the record may not read READY unless the resume-state closure is
	// complete.
	RequireStateComplete *bool `json:"requireStateComplete,omitempty"`
}

type CheckpointRecordStatus struct {
	MetadataDigest string                    `json:"metadataDigest,omitempty"`
	LatestMtime    string                    `json:"latestMtime,omitempty"`
	Complete       bool                      `json:"complete,omitempty"`
	Fresh          bool                      `json:"fresh,omitempty"`
	Readiness      CheckpointReadinessStatus `json:"readiness,omitempty"`
	Ranks          []CheckpointRankStatus    `json:"ranks,omitempty"`
	Conditions     []metav1.Condition        `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=sfcr
type CheckpointRecord struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              CheckpointRecordSpec   `json:"spec,omitempty"`
	Status            CheckpointRecordStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type CheckpointRecordList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []CheckpointRecord `json:"items"`
}
