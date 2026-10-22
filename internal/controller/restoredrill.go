package controller

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	k8svalidation "k8s.io/apimachinery/pkg/util/validation"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/targetready"
)

const restoreDrillPollInterval = 2 * time.Second
const restoreDrillDefaultTimeoutSeconds int64 = 600

type RestoreDrillReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *RestoreDrillReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var drill shardferryv1.RestoreDrill
	if err := r.Get(ctx, request.NamespacedName, &drill); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if drill.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	if restoreDrillTerminal(drill.Status.Phase) {
		return r.reconcileDrillOutcome(ctx, &drill)
	}
	if err := validateRestoreDrill(&drill); err != nil {
		return r.finishDrill(ctx, &drill, "Failed", "InvalidSpec", err.Error(), nil)
	}

	var record shardferryv1.CheckpointRecord
	if err := r.Get(ctx, client.ObjectKey{
		Namespace: drill.Namespace,
		Name:      drill.Spec.RecordRef,
	}, &record); err != nil {
		return r.finishDrill(
			ctx,
			&drill,
			"Failed",
			"RecordNotFound",
			fmt.Sprintf("get CheckpointRecord %q: %v", drill.Spec.RecordRef, err),
			nil,
		)
	}

	managedJob := drill.Spec.JobTemplate != nil
	jobName := strings.TrimSpace(drill.Spec.JobName)
	if managedJob {
		jobName = strings.TrimSpace(drill.Status.JobName)
		if jobName == "" {
			jobName = restoreDrillJobName(&drill)
		}
	}

	var job batchv1.Job
	if err := r.Get(ctx, client.ObjectKey{
		Namespace: drill.Namespace,
		Name:      jobName,
	}, &job); err != nil {
		if apierrors.IsNotFound(err) {
			if managedJob {
				desired, renderErr := renderRestoreDrillJob(
					&drill,
					&record,
					jobName,
				)
				if renderErr != nil {
					return r.finishDrill(
						ctx,
						&drill,
						"Failed",
						"InvalidJobTemplate",
						renderErr.Error(),
						nil,
					)
				}
				if createErr := r.Create(ctx, desired); createErr != nil &&
					!apierrors.IsAlreadyExists(createErr) {
					return r.finishDrill(
						ctx,
						&drill,
						"Failed",
						"JobCreateFailed",
						fmt.Sprintf("create restore drill Job %q: %v", jobName, createErr),
						nil,
					)
				}
				if createErr := r.Get(ctx, client.ObjectKey{
					Namespace: drill.Namespace,
					Name:      jobName,
				}, &job); createErr != nil {
					return ctrl.Result{}, createErr
				}
				if verifyErr := verifyManagedRestoreDrillJob(&drill, &job); verifyErr != nil {
					return r.finishDrillWithJob(
						ctx,
						&drill,
						"Failed",
						"JobConflict",
						verifyErr.Error(),
						jobName,
						restoreDrillStartedAt(&job, ""),
						nil,
					)
				}
				startedAt := restoreDrillStartedAt(&job, "")
				if _, err := r.setDrillStatusWithJob(
					ctx,
					&drill,
					"Running",
					"JobCreated",
					"created target Job "+jobName,
					jobName,
					startedAt,
					nil,
				); err != nil {
					return ctrl.Result{}, err
				}
				return ctrl.Result{RequeueAfter: restoreDrillPollInterval}, nil
			}
			return r.finishDrill(
				ctx,
				&drill,
				"Failed",
				"JobNotFound",
				fmt.Sprintf("target Job %q not found", jobName),
				nil,
			)
		}
		return ctrl.Result{}, err
	}
	if managedJob {
		if err := verifyManagedRestoreDrillJob(&drill, &job); err != nil {
			return r.finishDrillWithJob(
				ctx,
				&drill,
				"Failed",
				"JobConflict",
				err.Error(),
				jobName,
				restoreDrillStartedAt(&job, drill.Status.StartedAt),
				nil,
			)
		}
	}
	startedAt := restoreDrillStartedAt(&job, drill.Status.StartedAt)
	if failed, message := targetValidationJobFailed(&job); failed {
		return r.finishDrillWithJob(
			ctx,
			&drill,
			"Failed",
			"JobFailed",
			message,
			jobName,
			startedAt,
			nil,
		)
	}
	if !targetValidationJobComplete(&job) {
		if _, err := r.setDrillStatusWithJob(
			ctx,
			&drill,
			"Running",
			"Pending",
			"waiting for target Job "+jobName,
			jobName,
			startedAt,
			nil,
		); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: restoreDrillPollInterval}, nil
	}

	expected := make([]int32, drill.Spec.TargetWorldSize)
	for index := range expected {
		expected[index] = int32(index)
	}
	reports, issues := r.collectDrillReports(ctx, &drill, jobName, expected)
	if len(issues) > 0 {
		return r.finishDrillWithJob(
			ctx,
			&drill,
			"Failed",
			"InvalidReport",
			strings.Join(issues, "; "),
			jobName,
			startedAt,
			nil,
		)
	}
	evaluation := targetready.Evaluate(
		reports,
		expected,
		drill.Spec.Version,
		drill.Spec.Steps,
	)
	if drill.Spec.RequireStateComplete && !evaluation.StateComplete {
		return r.finishDrillWithJob(
			ctx,
			&drill,
			"Failed",
			"StateIncomplete",
			"target reports do not prove complete rank-local state",
			jobName,
			startedAt,
			&evaluation,
		)
	}
	phase := "Failed"
	if evaluation.Ready {
		phase = "Succeeded"
	}
	result, err := r.finishDrillWithJob(
		ctx,
		&drill,
		phase,
		evaluation.Condition,
		evaluation.Message,
		jobName,
		startedAt,
		&evaluation,
	)
	if err != nil {
		return result, err
	}
	return result, nil
}

func (r *RestoreDrillReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&shardferryv1.RestoreDrill{}).
		Owns(&batchv1.Job{}).
		Complete(r)
}

func validateRestoreDrill(drill *shardferryv1.RestoreDrill) error {
	if strings.TrimSpace(drill.Spec.RecordRef) == "" {
		return fmt.Errorf("recordRef is required")
	}
	hasJobName := strings.TrimSpace(drill.Spec.JobName) != ""
	hasJobTemplate := drill.Spec.JobTemplate != nil
	if hasJobName == hasJobTemplate {
		return fmt.Errorf("exactly one of jobName or jobTemplate is required")
	}
	if hasJobName {
		if errors := k8svalidation.IsDNS1123Subdomain(drill.Spec.JobName); len(errors) > 0 {
			return fmt.Errorf("invalid jobName: %s", strings.Join(errors, "; "))
		}
	}
	if strings.TrimSpace(drill.Spec.Version) == "" {
		return fmt.Errorf("version is required")
	}
	if drill.Spec.TargetWorldSize < 1 {
		return fmt.Errorf("targetWorldSize must be positive")
	}
	if drill.Spec.Steps < 1 {
		return fmt.Errorf("steps must be positive")
	}
	if drill.Spec.RequireStateComplete &&
		strings.TrimSpace(drill.Spec.StateTemplate) == "" {
		return fmt.Errorf("stateTemplate is required when requireStateComplete is true")
	}
	return nil
}

func (r *RestoreDrillReconciler) finishDrill(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
	phase string,
	reason string,
	message string,
	evaluation *targetready.Evaluation,
) (ctrl.Result, error) {
	return r.finishDrillWithJob(
		ctx,
		drill,
		phase,
		reason,
		message,
		drill.Spec.JobName,
		drill.Status.StartedAt,
		evaluation,
	)
}

func (r *RestoreDrillReconciler) finishDrillWithJob(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
	phase string,
	reason string,
	message string,
	jobName string,
	startedAt string,
	evaluation *targetready.Evaluation,
) (ctrl.Result, error) {
	if _, err := r.setDrillStatusWithJob(
		ctx,
		drill,
		phase,
		reason,
		message,
		jobName,
		startedAt,
		evaluation,
	); err != nil {
		return ctrl.Result{}, err
	}
	return r.reconcileDrillOutcome(ctx, drill)
}

func (r *RestoreDrillReconciler) setDrillStatus(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
	phase string,
	reason string,
	message string,
	evaluation *targetready.Evaluation,
) error {
	_, err := r.setDrillStatusWithJob(
		ctx,
		drill,
		phase,
		reason,
		message,
		"",
		"",
		evaluation,
	)
	return err
}

func (r *RestoreDrillReconciler) setDrillStatusWithJob(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
	phase string,
	reason string,
	message string,
	jobName string,
	startedAt string,
	evaluation *targetready.Evaluation,
) (bool, error) {
	before := drill.Status.DeepCopy()
	if jobName != "" {
		drill.Status.JobName = jobName
	}
	if drill.Status.StartedAt == "" && startedAt != "" {
		drill.Status.StartedAt = startedAt
	}
	if restoreDrillTerminal(phase) && drill.Status.CompletedAt == "" {
		drill.Status.CompletedAt = time.Now().UTC().Format(time.RFC3339)
	}
	drill.Status.Phase = phase
	drill.Status.Message = message
	if evaluation != nil {
		drill.Status.RanksReady = evaluation.RanksReady
		drill.Status.RanksTotal = evaluation.RanksTotal
		drill.Status.StepsRan = evaluation.StepsRan
		drill.Status.StateComplete = evaluation.StateComplete
		drill.Status.MissingRanks = append([]int32(nil), evaluation.MissingRanks...)
		drill.Status.ModelLoaded = len(evaluation.Reports) > 0
		drill.Status.OptimizerLoaded = len(evaluation.Reports) > 0
		for _, report := range evaluation.Reports {
			drill.Status.ModelLoaded = drill.Status.ModelLoaded && report.ModelLoaded
			drill.Status.OptimizerLoaded = drill.Status.OptimizerLoaded && report.OptimizerLoaded
		}
	}
	status := metav1.ConditionUnknown
	if phase == "Succeeded" {
		status = metav1.ConditionTrue
	}
	if phase == "Failed" {
		status = metav1.ConditionFalse
	}
	meta.SetStatusCondition(&drill.Status.Conditions, metav1.Condition{
		Type:               "DrillReady",
		Status:             status,
		Reason:             reason,
		Message:            message,
		ObservedGeneration: drill.Generation,
	})
	if reflect.DeepEqual(before, &drill.Status) {
		return false, nil
	}
	if err := r.Status().Update(ctx, drill); err != nil {
		return false, err
	}
	return true, nil
}

func restoreDrillTerminal(phase string) bool {
	return phase == "Succeeded" || phase == "Failed" || phase == "Cancelled"
}

func (r *RestoreDrillReconciler) reconcileDrillOutcome(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
) (ctrl.Result, error) {
	loadable, applies := restoreDrillRecordOutcome(drill)
	if !applies {
		return ctrl.Result{}, nil
	}
	var record shardferryv1.CheckpointRecord
	if err := r.Get(ctx, client.ObjectKey{
		Namespace: drill.Namespace,
		Name:      drill.Spec.RecordRef,
	}, &record); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	ref := restoreDrillRef(drill)
	if record.Status.Readiness.LastDrill == ref &&
		record.Status.Readiness.Loadable != nil &&
		*record.Status.Readiness.Loadable == loadable &&
		record.Status.Readiness.LastDrillVersion == drill.Spec.Version &&
		record.Status.Readiness.LastDrillDigest == record.Status.MetadataDigest {
		return ctrl.Result{}, nil
	}
	record.Status.Readiness.Loadable = &loadable
	record.Status.Readiness.LastDrill = ref
	record.Status.Readiness.LastDrillVersion = drill.Spec.Version
	record.Status.Readiness.LastDrillDigest = record.Status.MetadataDigest
	record.Status.Readiness.ObservedAt = time.Now().UTC().Format(time.RFC3339)
	if err := r.Status().Update(ctx, &record); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func restoreDrillRecordOutcome(
	drill *shardferryv1.RestoreDrill,
) (bool, bool) {
	switch drill.Status.Phase {
	case "Succeeded":
		return true, true
	case "Failed":
		condition := meta.FindStatusCondition(
			drill.Status.Conditions,
			"DrillReady",
		)
		if condition == nil {
			return false, true
		}
		switch condition.Reason {
		case "InvalidSpec",
			"InvalidJobTemplate",
			"JobCreateFailed",
			"JobConflict",
			"JobNotFound",
			"RecordNotFound":
			return false, false
		default:
			return false, true
		}
	default:
		return false, false
	}
}

func restoreDrillJobName(drill *shardferryv1.RestoreDrill) string {
	return deterministicJobName(drill.Name, "-restore-drill")
}

func restoreDrillUID(drill *shardferryv1.RestoreDrill) string {
	if drill.UID != "" {
		return string(drill.UID)
	}
	if drill.Namespace == "" {
		return drill.Name
	}
	return drill.Namespace + "/" + drill.Name
}

func restoreDrillRef(drill *shardferryv1.RestoreDrill) string {
	if drill.Namespace == "" {
		return drill.Name
	}
	return drill.Namespace + "/" + drill.Name
}

func restoreDrillStartedAt(job *batchv1.Job, existing string) string {
	if existing != "" {
		return existing
	}
	if job != nil && job.Status.StartTime != nil {
		return job.Status.StartTime.UTC().Format(time.RFC3339)
	}
	if job != nil && !job.CreationTimestamp.IsZero() {
		return job.CreationTimestamp.UTC().Format(time.RFC3339)
	}
	return time.Now().UTC().Format(time.RFC3339)
}

func renderRestoreDrillJob(
	drill *shardferryv1.RestoreDrill,
	record *shardferryv1.CheckpointRecord,
	jobName string,
) (*batchv1.Job, error) {
	payload, err := rawExtensionPayloadFor(
		drill.Spec.JobTemplate,
		"RestoreDrill.spec.jobTemplate",
	)
	if err != nil {
		return nil, err
	}
	var job batchv1.Job
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&job); err != nil {
		return nil, fmt.Errorf("decode RestoreDrill.spec.jobTemplate: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf(
			"RestoreDrill.spec.jobTemplate contains trailing data",
		)
	}
	if job.APIVersion != "batch/v1" || job.Kind != "Job" {
		return nil, fmt.Errorf(
			"RestoreDrill.spec.jobTemplate must be batch/v1 Job, got %s %s",
			job.APIVersion,
			job.Kind,
		)
	}
	if job.Namespace != "" && job.Namespace != drill.Namespace {
		return nil, fmt.Errorf(
			"restore drill Job namespace %q must match drill namespace %q",
			job.Namespace,
			drill.Namespace,
		)
	}
	if len(job.Spec.Template.Spec.Containers) == 0 {
		return nil, fmt.Errorf("restore drill Job has no containers")
	}
	if job.Spec.Suspend != nil && *job.Spec.Suspend {
		return nil, fmt.Errorf("restore drill Job must not be suspended")
	}
	indexed := batchv1.IndexedCompletion
	if job.Spec.CompletionMode != nil && *job.Spec.CompletionMode != indexed {
		return nil, fmt.Errorf("restore drill Job must use Indexed completion mode")
	}
	if job.Spec.Completions != nil &&
		*job.Spec.Completions != drill.Spec.TargetWorldSize {
		return nil, fmt.Errorf(
			"restore drill Job completions %d do not match targetWorldSize %d",
			*job.Spec.Completions,
			drill.Spec.TargetWorldSize,
		)
	}
	if job.Spec.Parallelism != nil &&
		*job.Spec.Parallelism != drill.Spec.TargetWorldSize {
		return nil, fmt.Errorf(
			"restore drill Job parallelism %d do not match targetWorldSize %d",
			*job.Spec.Parallelism,
			drill.Spec.TargetWorldSize,
		)
	}
	for _, owner := range job.OwnerReferences {
		if owner.Controller != nil && *owner.Controller {
			return nil, fmt.Errorf(
				"restore drill Job template must not set a controller ownerReference",
			)
		}
	}

	checkpointTemplate := strings.TrimSpace(drill.Spec.CheckpointTemplate)
	if checkpointTemplate == "" {
		checkpointTemplate = record.Spec.Checkpoint
	}
	if checkpointTemplate == "" {
		return nil, fmt.Errorf(
			"checkpointTemplate is required when the CheckpointRecord has no checkpoint path",
		)
	}

	job.TypeMeta = metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"}
	job.Namespace = drill.Namespace
	job.Name = jobName
	job.GenerateName = ""
	job.OwnerReferences = nil
	job.Spec.CompletionMode = &indexed
	job.Spec.Completions = int32Pointer(drill.Spec.TargetWorldSize)
	job.Spec.Parallelism = int32Pointer(drill.Spec.TargetWorldSize)
	job.Spec.BackoffLimit = int32Pointer(0)
	job.Spec.TTLSecondsAfterFinished = nil
	timeoutSeconds := drill.Spec.TimeoutSeconds
	if timeoutSeconds <= 0 {
		timeoutSeconds = restoreDrillDefaultTimeoutSeconds
	}
	job.Spec.ActiveDeadlineSeconds = &timeoutSeconds
	job.Spec.Template.Spec.RestartPolicy = corev1.RestartPolicyNever
	if job.Labels == nil {
		job.Labels = map[string]string{}
	}
	job.Labels["app.kubernetes.io/managed-by"] = "shardferry"
	job.Labels["shardferry.io/restore-drill"] = "true"
	if job.Annotations == nil {
		job.Annotations = map[string]string{}
	}
	job.Annotations["shardferry.io/restore-drill-uid"] = restoreDrillUID(drill)
	job.Annotations["shardferry.io/checkpoint-record"] = drill.Spec.RecordRef
	job.Annotations["shardferry.io/target-version"] = drill.Spec.Version
	if drill.UID != "" {
		controller := true
		blockOwnerDeletion := true
		job.OwnerReferences = []metav1.OwnerReference{{
			APIVersion:         shardferryv1.GroupVersion.String(),
			Kind:               "RestoreDrill",
			Name:               drill.Name,
			UID:                drill.UID,
			Controller:         &controller,
			BlockOwnerDeletion: &blockOwnerDeletion,
		}}
	}

	env := []corev1.EnvVar{
		{Name: "SHARDFERRY_TARGET_READY", Value: "1"},
		{Name: "SHARDFERRY_REPORT_SCHEMA_VERSION", Value: targetready.SchemaVersion},
		{Name: "SHARDFERRY_REPORT_PATH", Value: targetReadyReportPath},
		{Name: "SHARDFERRY_MIGRATION_UID", Value: restoreDrillUID(drill)},
		{Name: "SHARDFERRY_VERSION", Value: drill.Spec.Version},
		{Name: "SHARDFERRY_TARGET_WORLD_SIZE", Value: strconv.Itoa(
			int(drill.Spec.TargetWorldSize),
		)},
		{Name: "SHARDFERRY_VALIDATION_STEPS", Value: strconv.Itoa(
			int(drill.Spec.Steps),
		)},
		{Name: "SHARDFERRY_CHECKPOINT_TEMPLATE", Value: checkpointTemplate},
	}
	if drill.Spec.StateTemplate != "" {
		env = append(
			env,
			corev1.EnvVar{
				Name:  "SHARDFERRY_STATE_TEMPLATE",
				Value: drill.Spec.StateTemplate,
			},
			corev1.EnvVar{
				Name:  "SHARDFERRY_STATE_PROVIDERS",
				Value: strings.Join(drill.Spec.StateProviders, ","),
			},
			corev1.EnvVar{
				Name:  "SHARDFERRY_REQUIRE_STATE",
				Value: boolEnv(drill.Spec.RequireStateComplete),
			},
		)
	}
	for index := range job.Spec.Template.Spec.Containers {
		container := &job.Spec.Template.Spec.Containers[index]
		container.Env = upsertEnv(container.Env, env...)
		container.TerminationMessagePath = targetReadyReportPath
		container.TerminationMessagePolicy = corev1.TerminationMessageReadFile
	}
	return &job, nil
}

func verifyManagedRestoreDrillJob(
	drill *shardferryv1.RestoreDrill,
	job *batchv1.Job,
) error {
	uid := ""
	recordRef := ""
	version := ""
	if job.Annotations != nil {
		uid = job.Annotations["shardferry.io/restore-drill-uid"]
		recordRef = job.Annotations["shardferry.io/checkpoint-record"]
		version = job.Annotations["shardferry.io/target-version"]
	}
	if uid != restoreDrillUID(drill) {
		return fmt.Errorf(
			"restore drill Job %s belongs to drill UID %q, want %q",
			job.Name,
			uid,
			restoreDrillUID(drill),
		)
	}
	if recordRef != drill.Spec.RecordRef {
		return fmt.Errorf(
			"restore drill Job %s targets CheckpointRecord %q, want %q",
			job.Name,
			recordRef,
			drill.Spec.RecordRef,
		)
	}
	if version != drill.Spec.Version {
		return fmt.Errorf(
			"restore drill Job %s version %q, want %q",
			job.Name,
			version,
			drill.Spec.Version,
		)
	}
	if drill.UID == "" {
		return nil
	}
	for _, owner := range job.OwnerReferences {
		if owner.APIVersion == shardferryv1.GroupVersion.String() &&
			owner.Kind == "RestoreDrill" &&
			owner.Name == drill.Name &&
			owner.UID == drill.UID &&
			owner.Controller != nil &&
			*owner.Controller {
			return nil
		}
	}
	return fmt.Errorf(
		"restore drill Job %s is not controlled by RestoreDrill %s",
		job.Name,
		drill.Name,
	)
}

func (r *RestoreDrillReconciler) collectDrillReports(
	ctx context.Context,
	drill *shardferryv1.RestoreDrill,
	jobName string,
	expected []int32,
) ([]targetready.RankReport, []string) {
	helper := &Reconciler{Client: r.Client}
	pods, err := helper.targetValidationPods(
		ctx,
		drill.Namespace,
		jobName,
	)
	if err != nil {
		return nil, []string{err.Error()}
	}
	reports := make(map[int32]targetready.RankReport)
	var issues []string
	allowed := make(map[int32]struct{}, len(expected))
	for _, rank := range expected {
		allowed[rank] = struct{}{}
	}
	for _, pod := range pods {
		if pod.Status.Phase != corev1.PodSucceeded {
			continue
		}
		rank, err := targetReadyPodRank(pod, drill.Spec.TargetWorldSize)
		if err != nil {
			issues = append(issues, fmt.Sprintf("pod %s: %v", pod.Name, err))
			continue
		}
		if _, ok := allowed[rank]; !ok {
			issues = append(issues, fmt.Sprintf(
				"pod %s reported unexpected rank %d",
				pod.Name,
				rank,
			))
			continue
		}
		for _, status := range pod.Status.ContainerStatuses {
			if status.State.Terminated == nil {
				continue
			}
			message := strings.TrimSpace(status.State.Terminated.Message)
			if message == "" {
				continue
			}
			report, err := targetready.DecodeRankReport([]byte(message))
			if err != nil {
				issues = append(issues, fmt.Sprintf(
					"pod %s container %s: %v",
					pod.Name,
					status.Name,
					err,
				))
				continue
			}
			if report.MigrationUID != restoreDrillUID(drill) {
				issues = append(issues, fmt.Sprintf(
					"pod %s container %s: migrationUID %q does not match",
					pod.Name,
					status.Name,
					report.MigrationUID,
				))
				continue
			}
			if report.Rank != rank {
				issues = append(issues, fmt.Sprintf(
					"pod %s report rank %d does not match pod rank %d",
					pod.Name,
					report.Rank,
					rank,
				))
				continue
			}
			if previous, ok := reports[rank]; ok && previous != report {
				issues = append(issues, fmt.Sprintf(
					"conflicting reports for rank %d",
					rank,
				))
				continue
			}
			reports[rank] = report
		}
	}
	result := make([]targetready.RankReport, 0, len(reports))
	for _, report := range reports {
		result = append(result, report)
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Rank < result[j].Rank
	})
	return result, issues
}
