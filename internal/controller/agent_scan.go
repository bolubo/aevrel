package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

const maxAgentCheckpointResponseBytes int64 = 16 * 1024 * 1024

var reasonRankPattern = regexp.MustCompile(`(?i)rank\s+(\d+)`)

type agentCheckpointResponse struct {
	Version     string              `json:"version"`
	Record      agentCheckpointFact `json:"record"`
	ResumeState *agentResumeState   `json:"resumeState"`
}

// agentResumeState is the resume-state closure summary a version 2 agent
// reports next to its checkpoint fact. It answers one question: does this
// agent see a closed component set, and if so which manifest describes it?
//
// A nil *agentResumeState means "this agent has no closure evidence", which
// the controller must treat as an absence of proof, never as a closed set.
type agentResumeState struct {
	ManifestDigest string                 `json:"manifestDigest"`
	Closure        string                 `json:"closure"`
	Components     []agentResumeComponent `json:"components"`
	ReasonCodes    []string               `json:"reasonCodes"`
}

type agentResumeComponent struct {
	Kind     string `json:"kind"`
	Required bool   `json:"required"`
	Present  bool   `json:"present"`
}

var resumeManifestDigestPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

func (r *agentResumeState) validate() error {
	switch r.Closure {
	case "COMPLETE", "PARTIAL", "MISSING", "MALFORMED":
	default:
		return fmt.Errorf("unsupported resumeState closure %q", r.Closure)
	}
	if r.Closure == "MALFORMED" {
		// A manifest that did not parse cannot have a digest; saying so is
		// what keeps a broken file visible instead of looking like "none".
		if r.ManifestDigest != "" {
			return fmt.Errorf("a malformed resumeState must not carry a manifest digest")
		}
	} else if !resumeManifestDigestPattern.MatchString(r.ManifestDigest) {
		return fmt.Errorf("resumeState manifestDigest must be sha256")
	}
	seen := make(map[string]struct{}, len(r.Components))
	for _, component := range r.Components {
		if component.Kind == "" {
			return fmt.Errorf("resumeState component kind is required")
		}
		if _, ok := seen[component.Kind]; ok {
			return fmt.Errorf("duplicate resumeState component %q", component.Kind)
		}
		seen[component.Kind] = struct{}{}
	}
	return nil
}

// agreesWith reports whether two agents describe the same closure.
func (r *agentResumeState) agreesWith(other *agentResumeState) bool {
	if other == nil {
		return false
	}
	return r.Closure == other.Closure && r.ManifestDigest == other.ManifestDigest
}

type agentCheckpointFact struct {
	Checkpoint     string            `json:"checkpoint"`
	Namespace      string            `json:"namespace"`
	JobUID         string            `json:"job_uid"`
	Version        string            `json:"version"`
	Step           *int64            `json:"step,omitempty"`
	Provider       string            `json:"provider"`
	FormatVersion  string            `json:"format_version"`
	MetadataDigest string            `json:"metadata_digest"`
	LatestMtime    string            `json:"latest_mtime"`
	Complete       bool              `json:"complete"`
	Ranks          []agentRankFact   `json:"ranks"`
	Reasons        []agentReasonFact `json:"reasons"`
}

type agentRankFact struct {
	Rank   int32            `json:"rank"`
	Shards []agentShardFact `json:"shards"`
	Bytes  int64            `json:"bytes"`
}

type agentShardFact struct {
	Name         string `json:"name"`
	RelativePath string `json:"relative_path"`
	Rank         int32  `json:"rank"`
	Offset       int64  `json:"offset"`
	Length       int64  `json:"length"`
	SizeBytes    int64  `json:"size_bytes"`
}

type agentReasonFact struct {
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

type checkpointAgentObservation struct {
	Endpoint string
	Fact     agentCheckpointFact
	Resume   *agentResumeState
}

func (r *CheckpointReadinessReconciler) discoverAgent(
	ctx context.Context,
	record *aevrelv1.CheckpointRecord,
) error {
	if record.Spec.AgentEndpoint != "" && record.Spec.AgentSelector != nil {
		return fmt.Errorf("agentEndpoint and agentSelector are mutually exclusive")
	}
	endpoints, err := r.checkpointAgentEndpoints(ctx, record)
	if err != nil {
		return err
	}
	if len(endpoints) == 0 {
		return fmt.Errorf("no checkpoint agent endpoint is configured")
	}
	token, err := r.checkpointAgentToken(ctx, record)
	if err != nil {
		return err
	}
	observations := make([]checkpointAgentObservation, 0, len(endpoints))
	for _, endpoint := range endpoints {
		fact, resume, err := r.queryCheckpointAgent(ctx, record, endpoint, token)
		if err != nil {
			return fmt.Errorf("agent %s: %w", endpoint, err)
		}
		observations = append(observations, checkpointAgentObservation{
			Endpoint: endpoint,
			Fact:     fact,
			Resume:   resume,
		})
	}
	return applyAgentCheckpointFacts(record, observations)
}

func (r *CheckpointReadinessReconciler) checkpointAgentEndpoints(
	ctx context.Context,
	record *aevrelv1.CheckpointRecord,
) ([]string, error) {
	if endpoint := strings.TrimSpace(record.Spec.AgentEndpoint); endpoint != "" {
		return []string{strings.TrimRight(endpoint, "/")}, nil
	}
	selector := record.Spec.AgentSelector
	if selector == nil {
		return nil, nil
	}
	resolver := &AgentResolver{Client: r.Client}
	pods, err := resolver.agentPods(ctx, record.Namespace, selector)
	if err != nil {
		return nil, err
	}
	endpoints := make([]string, 0, len(pods))
	for _, pod := range pods {
		endpoint, err := agentEndpoint(pod, selector)
		if err != nil {
			return nil, fmt.Errorf("agent pod %s: %w", pod.Name, err)
		}
		endpoints = append(endpoints, endpoint)
	}
	sort.Strings(endpoints)
	return endpoints, nil
}

func (r *CheckpointReadinessReconciler) checkpointAgentToken(
	ctx context.Context,
	record *aevrelv1.CheckpointRecord,
) (string, error) {
	token := r.AgentToken
	if record.Spec.AgentSecretRef == nil {
		return token, nil
	}
	var secret corev1.Secret
	if err := r.Get(ctx, types.NamespacedName{
		Namespace: record.Namespace,
		Name:      record.Spec.AgentSecretRef.Name,
	}, &secret); err != nil {
		return "", fmt.Errorf("read agent secret: %w", err)
	}
	key := record.Spec.AgentSecretRef.Key
	if key == "" {
		key = "token"
	}
	value := secret.Data[key]
	if len(value) == 0 {
		return "", fmt.Errorf(
			"secret %s/%s key %s is empty",
			record.Namespace,
			record.Spec.AgentSecretRef.Name,
			key,
		)
	}
	return string(value), nil
}

func (r *CheckpointReadinessReconciler) queryCheckpointAgent(
	ctx context.Context,
	record *aevrelv1.CheckpointRecord,
	endpoint string,
	token string,
) (agentCheckpointFact, *agentResumeState, error) {
	values := url.Values{}
	if record.Namespace != "" {
		values.Set("namespace", record.Namespace)
	}
	values.Set("job_uid", record.Spec.JobRef)
	values.Set("version", record.Spec.Version)
	if record.Spec.Step != nil {
		values.Set("step", strconv.FormatInt(*record.Spec.Step, 10))
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		endpoint+"/v1/checkpoint-record?"+values.Encode(),
		nil,
	)
	if err != nil {
		return agentCheckpointFact{}, nil, err
	}
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	httpClient := r.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 10 * time.Second}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return agentCheckpointFact{}, nil, fmt.Errorf("query checkpoint record: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return agentCheckpointFact{}, nil, fmt.Errorf(
			"agent returned HTTP %d",
			response.StatusCode,
		)
	}
	var payload agentCheckpointResponse
	decoder := json.NewDecoder(io.LimitReader(
		response.Body,
		maxAgentCheckpointResponseBytes,
	))
	if err := decoder.Decode(&payload); err != nil {
		return agentCheckpointFact{}, nil, fmt.Errorf("decode checkpoint record: %w", err)
	}
	if payload.Version != "2" {
		return agentCheckpointFact{}, nil, fmt.Errorf(
			"unsupported checkpoint record version %q: a readiness agent must "+
				"report resume-state closure (version 2)",
			payload.Version,
		)
	}
	if payload.ResumeState != nil {
		if err := payload.ResumeState.validate(); err != nil {
			return agentCheckpointFact{}, nil, fmt.Errorf(
				"invalid resumeState from %s: %w",
				endpoint,
				err,
			)
		}
	}
	return payload.Record, payload.ResumeState, nil
}

func applyAgentCheckpointFacts(
	record *aevrelv1.CheckpointRecord,
	observations []checkpointAgentObservation,
) error {
	if len(observations) == 0 {
		return fmt.Errorf("no checkpoint agent observations")
	}
	reasons := make(map[string]aevrelv1.ControlReason)
	addReason := func(code, detail string) {
		key := code + "\x00" + detail
		reasons[key] = aevrelv1.ControlReason{Code: code, Detail: detail}
	}

	metadataDigest := ""
	provider := record.Spec.Provider
	formatVersion := record.Spec.FormatVersion
	latest := time.Time{}
	latestText := ""
	ranks := make(map[int32]map[string]aevrelv1.CheckpointShardStatus)
	agentReasons := make([]agentReasonFact, 0)

	for _, observation := range observations {
		fact := observation.Fact
		if fact.MetadataDigest == "" {
			addReason(
				"SOURCE_UNREADABLE",
				fmt.Sprintf("%s: metadata digest is empty", observation.Endpoint),
			)
		} else if metadataDigest == "" {
			metadataDigest = fact.MetadataDigest
		} else if fact.MetadataDigest != metadataDigest {
			addReason(
				"METADATA_MISMATCH",
				fmt.Sprintf(
					"%s reports digest %s, want %s",
					observation.Endpoint,
					fact.MetadataDigest,
					metadataDigest,
				),
			)
		}
		if fact.Provider != "" {
			if provider == "" {
				provider = fact.Provider
			} else if fact.Provider != provider {
				addReason(
					"PROVIDER_MISMATCH",
					fmt.Sprintf(
						"%s reports provider %s, want %s",
						observation.Endpoint,
						fact.Provider,
						provider,
					),
				)
			}
		}
		if fact.FormatVersion != "" {
			if formatVersion == "" {
				formatVersion = fact.FormatVersion
			} else if fact.FormatVersion != formatVersion {
				addReason(
					"FORMAT_VERSION_MISMATCH",
					fmt.Sprintf(
						"%s reports format %s, want %s",
						observation.Endpoint,
						fact.FormatVersion,
						formatVersion,
					),
				)
			}
		}
		if fact.Version != "" && fact.Version != record.Spec.Version {
			addReason(
				"VERSION_MISMATCH",
				fmt.Sprintf(
					"%s reports version %s, want %s",
					observation.Endpoint,
					fact.Version,
					record.Spec.Version,
				),
			)
		}
		if fact.LatestMtime != "" {
			parsed, err := time.Parse(time.RFC3339, fact.LatestMtime)
			if err != nil {
				addReason(
					"OBSERVATION_INVALID",
					fmt.Sprintf(
						"%s has invalid latest_mtime %q",
						observation.Endpoint,
						fact.LatestMtime,
					),
				)
			} else if latest.IsZero() || parsed.After(latest) {
				latest = parsed
				latestText = fact.LatestMtime
			}
		}
		for _, rank := range fact.Ranks {
			if rank.Rank < 0 || rank.Rank >= record.Spec.SourceWorldSize {
				addReason(
					"RANK_UNEXPECTED",
					fmt.Sprintf(
						"%s reports rank %d outside 0..%d",
						observation.Endpoint,
						rank.Rank,
						record.Spec.SourceWorldSize-1,
					),
				)
				continue
			}
			if ranks[rank.Rank] == nil {
				ranks[rank.Rank] = make(
					map[string]aevrelv1.CheckpointShardStatus,
				)
			}
			for _, shard := range rank.Shards {
				relativePath := shard.RelativePath
				if relativePath == "" {
					addReason(
						"OBSERVATION_INVALID",
						fmt.Sprintf(
							"%s rank %d has a shard without relative_path",
							observation.Endpoint,
							rank.Rank,
						),
					)
					continue
				}
				if shard.Rank != rank.Rank {
					addReason(
						"OBSERVATION_INVALID",
						fmt.Sprintf(
							"%s rank %d contains shard rank %d",
							observation.Endpoint,
							rank.Rank,
							shard.Rank,
						),
					)
					continue
				}
				converted := aevrelv1.CheckpointShardStatus{
					Name:         shard.Name,
					RelativePath: relativePath,
					Rank:         shard.Rank,
					Offset:       shard.Offset,
					Length:       shard.Length,
					SizeBytes:    shard.SizeBytes,
				}
				if previous, ok := ranks[rank.Rank][relativePath]; ok &&
					previous != converted {
					addReason(
						"RANK_DUPLICATE",
						fmt.Sprintf(
							"rank %d shard %s differs between agents",
							rank.Rank,
							relativePath,
						),
					)
					continue
				}
				ranks[rank.Rank][relativePath] = converted
			}
		}
		agentReasons = append(agentReasons, fact.Reasons...)
	}
	for _, reason := range agentReasons {
		if carryAgentReason(reason, ranks) {
			addReason(reason.Code, reason.Detail)
		}
	}

	resultRanks := make([]aevrelv1.CheckpointRankStatus, 0, len(ranks))
	for rank := int32(0); rank < record.Spec.SourceWorldSize; rank++ {
		shardsByPath, ok := ranks[rank]
		if !ok {
			addReason("RANK_MISSING", fmt.Sprintf("rank %d was not observed", rank))
			continue
		}
		shards := make(
			[]aevrelv1.CheckpointShardStatus,
			0,
			len(shardsByPath),
		)
		var bytes int64
		for _, shard := range shardsByPath {
			shards = append(shards, shard)
			bytes += shard.SizeBytes
		}
		sort.Slice(shards, func(i, j int) bool {
			if shards[i].RelativePath == shards[j].RelativePath {
				return shards[i].Name < shards[j].Name
			}
			return shards[i].RelativePath < shards[j].RelativePath
		})
		if len(shards) == 0 {
			addReason("SHARD_MISSING", fmt.Sprintf("rank %d has no shards", rank))
		}
		resultRanks = append(resultRanks, aevrelv1.CheckpointRankStatus{
			Rank:   rank,
			Shards: shards,
			Bytes:  bytes,
		})
	}

	resultReasons := make([]aevrelv1.ControlReason, 0, len(reasons))
	for _, reason := range reasons {
		resultReasons = append(resultReasons, reason)
	}
	sort.Slice(resultReasons, func(i, j int) bool {
		if resultReasons[i].Code == resultReasons[j].Code {
			return resultReasons[i].Detail < resultReasons[j].Detail
		}
		return resultReasons[i].Code < resultReasons[j].Code
	})

	record.Status.MetadataDigest = metadataDigest
	record.Status.LatestMtime = latestText
	record.Status.Complete = len(resultReasons) == 0
	record.Status.Ranks = resultRanks
	record.Status.Readiness.Reasons = resultReasons
	resume, mismatch := mergeResumeState(observations)
	if mismatch != "" {
		record.Status.Readiness.Reasons = append(
			record.Status.Readiness.Reasons,
			aevrelv1.ControlReason{Code: "RESUME_MISMATCH", Detail: mismatch},
		)
	}
	applyResumeState(&record.Status.Readiness, resume)
	return nil
}

// mergeResumeState picks the one closure summary every agent agrees on.
//
// Agents that see the same checkpoint must describe the same manifest. When
// they disagree the evidence is dropped rather than averaged, so no caller
// can pick a convenient verdict out of a split cluster.
func mergeResumeState(
	observations []checkpointAgentObservation,
) (*agentResumeState, string) {
	var agreed *agentResumeState
	for _, observation := range observations {
		if observation.Resume == nil {
			continue
		}
		if agreed == nil {
			agreed = observation.Resume
			continue
		}
		if !agreed.agreesWith(observation.Resume) {
			return nil, fmt.Sprintf(
				"%s reports closure %s digest %s, other agents report %s digest %s",
				observation.Endpoint,
				observation.Resume.Closure,
				observation.Resume.ManifestDigest,
				agreed.Closure,
				agreed.ManifestDigest,
			)
		}
	}
	return agreed, ""
}

// applyResumeState stores the closure evidence the controller will assess.
// An absent summary clears the fields so a stale verdict cannot survive an
// agent that stops reporting it.
func applyResumeState(
	status *aevrelv1.CheckpointReadinessStatus,
	resume *agentResumeState,
) {
	if resume == nil {
		status.ResumeClosure = ""
		status.ResumeManifestDigest = ""
		status.ResumeReasonCodes = nil
		return
	}
	codes := make([]string, 0, len(resume.ReasonCodes))
	for _, code := range resume.ReasonCodes {
		if code != "" {
			codes = append(codes, code)
		}
	}
	sort.Strings(codes)
	status.ResumeClosure = resume.Closure
	status.ResumeManifestDigest = resume.ManifestDigest
	status.ResumeReasonCodes = codes
}

func carryAgentReason(
	reason agentReasonFact,
	merged map[int32]map[string]aevrelv1.CheckpointShardStatus,
) bool {
	switch reason.Code {
	case "RANK_MISSING":
		return false
	case "SHARD_MISSING":
		rank, ok := reasonRank(reason.Detail)
		if !ok {
			return true
		}
		relativePath := reasonShardPath(reason.Detail)
		if relativePath == "" {
			return true
		}
		_, covered := merged[rank][relativePath]
		return !covered
	default:
		return true
	}
}

func reasonRank(detail string) (int32, bool) {
	match := reasonRankPattern.FindStringSubmatch(detail)
	if len(match) != 2 {
		return 0, false
	}
	value, err := strconv.ParseInt(match[1], 10, 32)
	if err != nil {
		return 0, false
	}
	return int32(value), true
}

func reasonShardPath(detail string) string {
	if index := strings.Index(detail, " (rank "); index >= 0 {
		return strings.TrimSpace(detail[:index])
	}
	return ""
}
