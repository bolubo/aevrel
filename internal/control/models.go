package control

import "time"

type ReadinessStatus string

const (
	ReadinessUnknown       ReadinessStatus = "UNKNOWN"
	ReadinessIncomplete    ReadinessStatus = "INCOMPLETE"
	ReadinessStale         ReadinessStatus = "STALE"
	ReadinessUnverified    ReadinessStatus = "UNVERIFIED"
	ReadinessReady         ReadinessStatus = "READY"
	ReadinessTargetBlocked ReadinessStatus = "TARGET_BLOCKED"
)

const (
	ReasonMetadataMissing   = "METADATA_MISSING"
	ReasonSourceUnreadable  = "SOURCE_UNREADABLE"
	ReasonRankMissing       = "RANK_MISSING"
	ReasonRankDuplicate     = "RANK_DUPLICATE"
	ReasonShardMissing      = "SHARD_MISSING"
	ReasonShardTruncated    = "SHARD_TRUNCATED"
	ReasonStale             = "STALE"
	ReasonDrillMissing      = "DRILL_MISSING"
	ReasonDrillFailed       = "DRILL_FAILED"
	ReasonTargetUnspecified = "TARGET_UNSPECIFIED"
	ReasonTargetBlocked     = "TARGET_BLOCKED"

	ReasonStateClosureIncomplete = "STATE_CLOSURE_INCOMPLETE"
	ReasonResumeDigestMismatch   = "RESUME_COMPONENT_DIGEST_MISMATCH"
)

type Reason struct {
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

type ShardRef struct {
	Name         string `json:"name"`
	RelativePath string `json:"relative_path"`
	Rank         int    `json:"rank"`
	Offset       int64  `json:"offset"`
	Length       int64  `json:"length"`
	SizeBytes    int64  `json:"size_bytes"`
}

type RankInventory struct {
	Rank   int        `json:"rank"`
	Shards []ShardRef `json:"shards"`
	Bytes  int64      `json:"bytes"`
}

type CheckpointRecord struct {
	Checkpoint     string          `json:"checkpoint"`
	Namespace      string          `json:"namespace"`
	JobUID         string          `json:"job_uid"`
	Version        string          `json:"version"`
	Step           *int64          `json:"step,omitempty"`
	Provider       string          `json:"provider"`
	FormatVersion  string          `json:"format_version"`
	MetadataDigest string          `json:"metadata_digest"`
	Ranks          []RankInventory `json:"ranks"`
	ObservedAt     string          `json:"observed_at"`
	LatestMtime    string          `json:"latest_mtime,omitempty"`
	CreatedAt      string          `json:"created_at,omitempty"`
	Complete       bool            `json:"complete"`
	Reasons        []Reason        `json:"reasons,omitempty"`
}

type DrillOutcome struct {
	Phase     string `json:"phase"`
	Succeeded bool   `json:"succeeded"`
	StepsRan  int    `json:"steps_ran"`
	ProofRef  string `json:"proof_ref,omitempty"`
	Detail    string `json:"detail,omitempty"`
	// ResumeManifestDigests lists the distinct resume-state manifest digests
	// the drill ranks actually restored; empty means no closure evidence.
	ResumeManifestDigests []string `json:"resume_manifest_digests,omitempty"`
}

type CheckpointReadiness struct {
	Status              ReadinessStatus `json:"status"`
	Complete            bool            `json:"complete"`
	Fresh               bool            `json:"fresh"`
	Loadable            *bool           `json:"loadable"`
	Movable             *bool           `json:"movable"`
	StateComplete       *bool           `json:"state_complete"`
	ReadinessLevel      string          `json:"readiness_level,omitempty"`
	Reasons             []Reason        `json:"reasons,omitempty"`
	ObservedAt          string          `json:"observed_at"`
	TargetWorldSize     *int            `json:"target_world_size,omitempty"`
	EstimatedRTOSeconds *int64          `json:"estimated_rto_seconds,omitempty"`
	LastDrill           string          `json:"last_drill,omitempty"`
	LastDrillDigest     string          `json:"last_drill_digest,omitempty"`
}

type RestoreDrill struct {
	RecordVersion   string    `json:"record_version"`
	TargetWorldSize int       `json:"target_world_size"`
	Steps           int       `json:"steps"`
	Phase           string    `json:"phase"`
	StartedAt       time.Time `json:"started_at"`
	FinishedAt      time.Time `json:"finished_at,omitempty"`
	ProofRef        string    `json:"proof_ref,omitempty"`
	Reasons         []Reason  `json:"reasons,omitempty"`
}

type RescueAction string

const (
	RescueRestore RescueAction = "restore"
	RescueRestart RescueAction = "restart"
	RescueMigrate RescueAction = "migrate"
	RescueHold    RescueAction = "hold"
)

type RescueDecision struct {
	Event     string       `json:"event"`
	Action    RescueAction `json:"action"`
	Code      string       `json:"code,omitempty"`
	Reason    string       `json:"reason"`
	Target    string       `json:"target,omitempty"`
	ProofRef  string       `json:"proof_ref,omitempty"`
	DecidedAt string       `json:"decided_at,omitempty"`
}
