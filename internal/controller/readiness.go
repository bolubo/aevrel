package controller

import (
	"context"
	"fmt"
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
		Record:               modelRecord,
		Now:                  now,
		TargetWorldSize:      controlTargetWorld(record.Status.Readiness.TargetWorldSize),
		Drill:                drill,
		State:                controlStateClosure(resumeClosure, resumeDigest, resumeCodes),
		RequireStateComplete: requireStateComplete(record),
	})

	before := record.Status.DeepCopy()
	record.Status.Complete = readiness.Complete
	record.Status.Fresh = readiness.Fresh
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:               string(readiness.Status),
		Complete:             readiness.Complete,
		Fresh:                readiness.Fresh,
		Loadable:             readiness.Loadable,
		Movable:              readiness.Movable,
		Reasons:              apiReasons(readiness.Reasons),
		ObservedAt:           readiness.ObservedAt,
		TargetWorldSize:      apiTargetWorld(readiness.TargetWorldSize),
		EstimatedRTOSeconds:  readiness.EstimatedRTOSeconds,
		LastDrill:            readiness.LastDrill,
		LastDrillVersion:     lastDrillVersion,
		LastDrillDigest:      lastDrillDigest,
		StateComplete:        readiness.StateComplete,
		ReadinessLevel:       readiness.ReadinessLevel,
		ResumeClosure:        resumeClosure,
		ResumeManifestDigest: resumeDigest,
		ResumeReasonCodes:    resumeCodes,
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

// controlStateClosure turns the closure evidence stored on the record into
// the control-plane summary.
//
// No evidence stays nil: the assessment then reports stateComplete=nil and
// can never claim L4, which is what keeps "the agent did not look" distinct
// from "the agent looked and the closure is incomplete".
func controlStateClosure(
	closure string,
	manifestDigest string,
	codes []string,
) *control.StateClosureSummary {
	if closure == "" {
		return nil
	}
	return &control.StateClosureSummary{
		Status:         closure,
		Closed:         closure == control.ClosureComplete,
		Codes:          append([]string(nil), codes...),
		ManifestDigest: manifestDigest,
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
		ProofRef:              readiness.LastDrill,
		ResumeManifestDigests: digests,
	}
}

func controlDrillForRecord(
	readiness aevrelv1.CheckpointReadinessStatus,
	version string,
	metadataDigest string,
) *control.DrillOutcome {
	drill := controlDrill(readiness)
	if drill == nil {
		return nil
	}
	if readiness.LastDrillVersion != version ||
		readiness.LastDrillDigest != metadataDigest {
		digests := []string{}
		if readiness.LastDrillDigest != "" {
			digests = []string{readiness.LastDrillDigest}
		}
		return &control.DrillOutcome{
			Phase:                 "Failed",
			Succeeded:             false,
			ProofRef:              readiness.LastDrill,
			ResumeManifestDigests: digests,
			Detail: fmt.Sprintf(
				"restore drill evidence is for version %q digest %q, current version %q digest %q",
				readiness.LastDrillVersion,
				readiness.LastDrillDigest,
				version,
				metadataDigest,
			),
		}
	}
	return drill
}
