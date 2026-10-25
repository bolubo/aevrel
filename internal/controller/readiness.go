package controller

import (
	"context"
	"net/http"
	"reflect"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

type CheckpointReadinessReconciler struct {
	client.Client
	Scheme     *runtime.Scheme
	AgentToken string
	HTTPClient *http.Client
}

func (r *CheckpointReadinessReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var record aevrelv1.CheckpointRecord
	if err := r.Get(ctx, request.NamespacedName, &record); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if record.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	if record.Spec.AgentEndpoint != "" || record.Spec.AgentSelector != nil {
		if err := r.discoverAgent(ctx, &record); err != nil {
			record.Status.Readiness.Status = aevrelv1.ReadinessUnknown
			record.Status.Readiness.Complete = false
			record.Status.Readiness.Fresh = false
			record.Status.Readiness.Loadable = nil
			record.Status.Readiness.Movable = nil
			record.Status.Readiness.ObservedAt = time.Now().UTC().Format(time.RFC3339)
			record.Status.Readiness.Reasons = []aevrelv1.ControlReason{{
				Code:   "AGENT_UNREACHABLE",
				Detail: err.Error(),
			}}
			meta.SetStatusCondition(&record.Status.Conditions, metav1.Condition{
				Type:               "CheckpointReady",
				Status:             metav1.ConditionFalse,
				Reason:             "AgentUnavailable",
				Message:            err.Error(),
				ObservedGeneration: record.Generation,
			})
			if err := r.Status().Update(ctx, &record); err != nil {
				return ctrl.Result{}, err
			}
			return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
		}
	}

	now := time.Now().UTC()
	lastDrillVersion := record.Status.Readiness.LastDrillVersion
	lastDrillDigest := record.Status.Readiness.LastDrillDigest
	resumeClosure := record.Status.Readiness.ResumeClosure
	resumeDigest := record.Status.Readiness.ResumeManifestDigest
	resumeCodes := append(
		[]string(nil),
		record.Status.Readiness.ResumeReasonCodes...,
	)
	resumeUID := record.Status.Readiness.ResumeCheckpointUID
	resumeVersion := record.Status.Readiness.ResumeCheckpointVersion
	resumeMetadataDigest := record.Status.Readiness.ResumeCheckpointMetadataDigest
	modelRecord := control.CheckpointRecord{
		Checkpoint:     record.Spec.Checkpoint,
		Namespace:      record.Namespace,
		JobUID:         record.Spec.JobRef,
		Version:        record.Spec.Version,
		Provider:       record.Spec.Provider,
		FormatVersion:  record.Spec.FormatVersion,
		MetadataDigest: record.Status.MetadataDigest,
		LatestMtime:    record.Status.LatestMtime,
		Complete:       record.Status.Complete,
		Ranks:          controlRanks(record.Status.Ranks),
		Reasons:        controlReasons(record.Status.Readiness.Reasons),
	}
	drill := controlDrillForRecord(
		record.Status.Readiness,
		record.Spec.Version,
		record.Status.MetadataDigest,
	)
	readiness := control.AssessReadiness(control.ReadinessInput{
		Record:          modelRecord,
		Now:             now,
		TargetWorldSize: controlTargetWorld(record.Status.Readiness.TargetWorldSize),
		Drill:           drill,
		State: controlStateClosure(resumeStateEvidence{
			closure:        resumeClosure,
			manifestDigest: resumeDigest,
			codes:          resumeCodes,
			checkpointUID:  resumeUID,
			version:        resumeVersion,
			metadataDigest: resumeMetadataDigest,
		}),
		RequireStateComplete: requireStateComplete(record),
	})

	before := record.Status.DeepCopy()
	previousReadiness := record.Status.Readiness
	record.Status.Complete = readiness.Complete
	record.Status.Fresh = readiness.Fresh
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:          string(readiness.Status),
		Complete:        readiness.Complete,
		Fresh:           readiness.Fresh,
		Loadable:        readiness.Loadable,
		Movable:         readiness.Movable,
		Reasons:         apiReasons(readiness.Reasons),
		ObservedAt:      readiness.ObservedAt,
		TargetWorldSize: apiTargetWorld(readiness.TargetWorldSize),
		// The drill evidence describes the last drill, not this assessment,
		// so it is carried across instead of recomputed.
		LastDrillSteps:                 previousReadiness.LastDrillSteps,
		LastDrillRanksReported:         previousReadiness.LastDrillRanksReported,
		LastDrillRanksExpected:         previousReadiness.LastDrillRanksExpected,
		EstimatedRTOSeconds:            readiness.EstimatedRTOSeconds,
		LastDrill:                      readiness.LastDrill,
		LastDrillVersion:               lastDrillVersion,
		LastDrillDigest:                lastDrillDigest,
		StateComplete:                  readiness.StateComplete,
		ReadinessLevel:                 readiness.ReadinessLevel,
		ResumeClosure:                  resumeClosure,
		ResumeManifestDigest:           resumeDigest,
		ResumeReasonCodes:              resumeCodes,
		ResumeCheckpointUID:            resumeUID,
		ResumeCheckpointVersion:        resumeVersion,
		ResumeCheckpointMetadataDigest: resumeMetadataDigest,
	}
	status := metav1.ConditionFalse
	reason := "NotReady"
	if readiness.Status == control.ReadinessReady {
		status = metav1.ConditionTrue
		reason = "Ready"
	}
	meta.SetStatusCondition(&record.Status.Conditions, metav1.Condition{
		Type:               "CheckpointReady",
		Status:             status,
		Reason:             reason,
		Message:            string(readiness.Status),
		ObservedGeneration: record.Generation,
	})
	if reflect.DeepEqual(before, &record.Status) {
		return ctrl.Result{}, nil
	}
	if err := r.Status().Update(ctx, &record); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

func (r *CheckpointReadinessReconciler) SetupWithManager(
	mgr ctrl.Manager,
) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&aevrelv1.CheckpointRecord{}).
		Complete(r)
}

func controlRanks(
	ranks []aevrelv1.CheckpointRankStatus,
) []control.RankInventory {
	result := make([]control.RankInventory, 0, len(ranks))
	for _, rank := range ranks {
		shards := make([]control.ShardRef, 0, len(rank.Shards))
		for _, shard := range rank.Shards {
			shards = append(shards, control.ShardRef{
				Name:         shard.Name,
				RelativePath: shard.RelativePath,
				Rank:         int(shard.Rank),
				Offset:       shard.Offset,
				Length:       shard.Length,
				SizeBytes:    shard.SizeBytes,
			})
		}
		result = append(result, control.RankInventory{
			Rank:   int(rank.Rank),
			Shards: shards,
			Bytes:  rank.Bytes,
		})
	}
	return result
}

func controlReasons(reasons []aevrelv1.ControlReason) []control.Reason {
	result := make([]control.Reason, 0, len(reasons))
	for _, reason := range reasons {
		result = append(result, control.Reason{
			Code:   reason.Code,
			Detail: reason.Detail,
		})
	}
	return result
}

func apiReasons(reasons []control.Reason) []aevrelv1.ControlReason {
	result := make([]aevrelv1.ControlReason, 0, len(reasons))
	for _, reason := range reasons {
		result = append(result, aevrelv1.ControlReason{
			Code:   reason.Code,
			Detail: reason.Detail,
		})
	}
	return result
}

func controlTargetWorld(value *int32) *int {
	if value == nil {
		return nil
	}
	converted := int(*value)
	return &converted
}

func apiTargetWorld(value *int) *int32 {
	if value == nil {
		return nil
	}
	converted := int32(*value)
	return &converted
}

// resumeStateEvidence is the closure evidence stored on the record, gathered
// into one value so the projection into the control plane cannot silently drop
// a field as the evidence grows.
type resumeStateEvidence struct {
	closure        string
	manifestDigest string
	codes          []string
	checkpointUID  string
	version        string
	metadataDigest string
}

// controlStateClosure turns the closure evidence stored on the record into
// the control-plane summary.
//
// No evidence stays nil: the assessment then reports stateComplete=nil and
// can never claim L4, which is what keeps "the agent did not look" distinct
// from "the agent looked and the closure is incomplete".
func controlStateClosure(
	evidence resumeStateEvidence,
) *control.StateClosureSummary {
	if evidence.closure == "" {
		return nil
	}
	return &control.StateClosureSummary{
		Status:                   evidence.closure,
		Closed:                   evidence.closure == control.ClosureComplete,
		Codes:                    append([]string(nil), evidence.codes...),
		ManifestDigest:           evidence.manifestDigest,
		CheckpointUID:            evidence.checkpointUID,
		CheckpointVersion:        evidence.version,
		CheckpointMetadataDigest: evidence.metadataDigest,
	}
}

func requireStateComplete(record aevrelv1.CheckpointRecord) bool {
	return record.Spec.RequireStateComplete != nil &&
		*record.Spec.RequireStateComplete
}

func controlDrill(
	readiness aevrelv1.CheckpointReadinessStatus,
) *control.DrillOutcome {
	if readiness.LastDrill == "" || readiness.Loadable == nil {
		return nil
	}
	phase := "Failed"
	if *readiness.Loadable {
		phase = "Succeeded"
	}
	digests := []string{}
	if readiness.LastDrillDigest != "" {
		digests = []string{readiness.LastDrillDigest}
	}
	return &control.DrillOutcome{
		Phase:                 phase,
		Succeeded:             *readiness.Loadable,
		StepsRan:              int(readiness.LastDrillSteps),
		RanksReported:         controlRanksReported(readiness.LastDrillRanksReported),
		RanksExpected:         int(readiness.LastDrillRanksExpected),
		ProofRef:              readiness.LastDrill,
		ResumeManifestDigests: digests,
		// The proof was produced for the checkpoint the drill saw, not for
		// whatever the record points at now. Carrying that identity lets the
		// assessment withdraw the claim when the checkpoint has moved on.
		CheckpointVersion:        readiness.LastDrillVersion,
		CheckpointMetadataDigest: readiness.LastDrillDigest,
	}
}

func controlRanksReported(ranks []int32) []int {
	if len(ranks) == 0 {
		return nil
	}
	reported := make([]int, 0, len(ranks))
	for _, rank := range ranks {
		reported = append(reported, int(rank))
	}
	return reported
}

// controlDrillForRecord passes the drill's own identity through unchanged.
//
// It used to pre-fail the drill when the record had moved to another version
// or digest. That collapsed two different situations into one generic
// DRILL_FAILED: "the drill failed" and "the drill proved a checkpoint we no
// longer have". The assessment now compares identities itself and reports
// DRILL_PROOF_STALE, so a consumer can tell a broken drill from a stale proof.
func controlDrillForRecord(
	readiness aevrelv1.CheckpointReadinessStatus,
	_ string,
	_ string,
) *control.DrillOutcome {
	return controlDrill(readiness)
}
