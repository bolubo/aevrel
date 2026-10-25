package controller

import (
	"context"
	"errors"
	"fmt"
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
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/targetready"
)

const targetReadyPendingRequeue = 2 * time.Second

type targetValidationConfigError struct {
	err error
}

func (e *targetValidationConfigError) Error() string {
	return e.err.Error()
}

func (e *targetValidationConfigError) Unwrap() error {
	return e.err
}

func invalidTargetJob(err error) error {
	return &targetValidationConfigError{err: err}
}

func isTargetValidationConfigError(err error) bool {
	var configErr *targetValidationConfigError
	return errors.As(err, &configErr)
}

func (r *Reconciler) reconcileTargetReady(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
) (ctrl.Result, error) {
	var latest aevrelv1.CheckpointMigration
	if err := r.Get(ctx, client.ObjectKeyFromObject(migration), &latest); err != nil {
		return ctrl.Result{}, err
	}
	if latest.Status.Phase == aevrelv1.MigrationPhaseTargetFailed ||
		latest.Status.Phase == aevrelv1.MigrationPhaseSucceeded {
		return ctrl.Result{}, nil
	}
	settings, enabled, err := parseTargetValidationSettings(&latest)
	if !enabled {
		return ctrl.Result{}, nil
	}
	if err != nil {
		return r.finishTargetReady(
			ctx,
			&latest,
			targetValidationSettings{},
			"",
			"",
			"InvalidJob",
			err.Error(),
			targetready.Evaluation{RanksTotal: latest.Spec.Target.WorldSize},
		)
	}
	expectedRanks, err := targetValidationRanks(&latest)
	if err != nil {
		return r.finishTargetReady(
			ctx,
			&latest,
			settings,
			settings.JobName,
			"",
			"InvalidJob",
			err.Error(),
			targetready.Evaluation{RanksTotal: latest.Spec.Target.WorldSize},
		)
	}

	jobName := settings.JobName
	if jobName == "" {
		jobName = targetValidationJobName(&latest)
	}
	startedAt := ""
	if latest.Status.TargetReady != nil {
		startedAt = latest.Status.TargetReady.StartedAt
	}
	if startedAt == "" {
		startedAt = time.Now().UTC().Format(time.RFC3339)
	}

	var job *batchv1.Job
	if settings.HasJobTemplate {
		job, err = r.ensureTargetValidationJob(ctx, &latest, settings)
		if err != nil {
			if !isTargetValidationConfigError(err) {
				return ctrl.Result{}, err
			}
			return r.finishTargetReady(
				ctx,
				&latest,
				settings,
				jobName,
				startedAt,
				"JobFailed",
				err.Error(),
				targetready.Evaluation{
					RanksTotal: int32(len(expectedRanks)),
				},
			)
		}
	} else {
		job = &batchv1.Job{}
		if err := r.Get(ctx, types.NamespacedName{
			Namespace: latest.Namespace,
			Name:      settings.JobName,
		}, job); err != nil {
			if !apierrors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			return r.finishTargetReady(
				ctx,
				&latest,
				settings,
				jobName,
				startedAt,
				"JobFailed",
				fmt.Sprintf("get target validation Job %s: %v", settings.JobName, err),
				targetready.Evaluation{
					RanksTotal: int32(len(expectedRanks)),
				},
			)
		}
		if err := verifyTargetValidationJob(&latest, job, settings.Version); err != nil {
			return r.finishTargetReady(
				ctx,
				&latest,
				settings,
				jobName,
				startedAt,
				"InvalidJob",
				err.Error(),
				targetready.Evaluation{
					RanksTotal: int32(len(expectedRanks)),
				},
			)
		}
	}

	status, err := r.updateTargetReadyStatus(
		ctx,
		&latest,
		func(current *aevrelv1.CheckpointMigration) bool {
			before := current.Status.DeepCopy()
			target := ensureTargetReadyStatus(current)
			target.Condition = "Pending"
			target.Ready = false
			target.Version = settings.Version
			target.RanksTotal = int32(len(expectedRanks))
			target.JobName = jobName
			target.StartedAt = startedAt
			target.Message = "waiting for target validation Job"
			current.Status.Phase = aevrelv1.MigrationPhaseTargetValidating
			meta.SetStatusCondition(&current.Status.Conditions, metav1.Condition{
				Type:               targetReadyCondition,
				Status:             metav1.ConditionUnknown,
				Reason:             "Pending",
				Message:            target.Message,
				ObservedGeneration: current.Generation,
			})
			return !reflect.DeepEqual(before, &current.Status)
		},
	)
	if err != nil {
		return ctrl.Result{}, err
	}

	start, err := time.Parse(time.RFC3339, startedAt)
	if err == nil && time.Since(start) > settings.Timeout {
		return r.finishTargetReady(
			ctx,
			status,
			settings,
			jobName,
			startedAt,
			"Timeout",
			fmt.Sprintf(
				"target validation exceeded %s",
				settings.Timeout,
			),
			targetready.Evaluation{
				RanksTotal: int32(len(expectedRanks)),
			},
		)
	}
	if failed, message := targetValidationJobFailed(job); failed {
		return r.finishTargetReady(
			ctx,
			status,
			settings,
			jobName,
			startedAt,
			"JobFailed",
			message,
			targetready.Evaluation{
				RanksTotal: int32(len(expectedRanks)),
			},
		)
	}
	if !targetValidationJobComplete(job) {
		if r.Recorder != nil {
			r.Recorder.Event(
				status,
				corev1.EventTypeNormal,
				"TargetValidationPending",
				"waiting for target validation Job "+jobName,
			)
		}
		return ctrl.Result{RequeueAfter: targetReadyPendingRequeue}, nil
	}

	reports, issues, err := r.collectTargetReadyReports(
		ctx,
		status,
		jobName,
		expectedRanks,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(issues) > 0 {
		return r.finishTargetReady(
			ctx,
			status,
			settings,
			jobName,
			startedAt,
			"InvalidReport",
			strings.Join(issues, "; "),
			targetready.Evaluation{
				Condition:  "InvalidReport",
				Message:    strings.Join(issues, "; "),
				RanksTotal: int32(len(expectedRanks)),
				Reports:    reports,
			},
		)
	}
	evaluation := targetready.Evaluate(targetready.EvaluateOptions{
		Reports:              reports,
		ExpectedRanks:        expectedRanks,
		Version:              settings.Version,
		MinSteps:             settings.Steps,
		RequireStateComplete: liveRequiresState(migration),
	})
	return r.finishTargetReady(
		ctx,
		status,
		settings,
		jobName,
		startedAt,
		evaluation.Condition,
		evaluation.Message,
		evaluation,
	)
}

func (r *Reconciler) ensureTargetValidationJob(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	settings targetValidationSettings,
) (*batchv1.Job, error) {
	desired, err := renderTargetValidationJob(migration, settings)
	if err != nil {
		return nil, invalidTargetJob(err)
	}
	var existing batchv1.Job
	err = r.Get(ctx, client.ObjectKeyFromObject(desired), &existing)
	if err == nil {
		if err := verifyTargetValidationJob(migration, &existing, settings.Version); err != nil {
			return nil, invalidTargetJob(err)
		}
		return &existing, nil
	}
	if !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("get target validation Job: %w", err)
	}
	if err := r.Create(ctx, desired); err != nil {
		if apierrors.IsAlreadyExists(err) {
			if err := r.Get(ctx, client.ObjectKeyFromObject(desired), &existing); err != nil {
				return nil, fmt.Errorf("get existing target validation Job: %w", err)
			}
			if err := verifyTargetValidationJob(migration, &existing, settings.Version); err != nil {
				return nil, invalidTargetJob(err)
			}
			return &existing, nil
		}
		if apierrors.IsInvalid(err) || apierrors.IsBadRequest(err) {
			return nil, invalidTargetJob(
				fmt.Errorf("create target validation Job: %w", err),
			)
		}
		return nil, fmt.Errorf("create target validation Job: %w", err)
	}
	if r.Recorder != nil {
		r.Recorder.Event(
			migration,
			corev1.EventTypeNormal,
			"TargetValidationCreated",
			"created target validation Job "+desired.Name,
		)
	}
	return desired, nil
}

func verifyTargetValidationJob(
	migration *aevrelv1.CheckpointMigration,
	job *batchv1.Job,
	version string,
) error {
	uid := ""
	jobVersion := ""
	if job.Annotations != nil {
		uid = job.Annotations["aevrel.dev/migration-uid"]
		jobVersion = job.Annotations["aevrel.dev/target-version"]
	}
	if uid != migrationUID(migration) {
		return fmt.Errorf(
			"target validation Job %s belongs to migration UID %q, want %q",
			job.Name,
			uid,
			migrationUID(migration),
		)
	}
	if jobVersion != version {
		return fmt.Errorf(
			"target validation Job %s version %q, want %q",
			job.Name,
			jobVersion,
			version,
		)
	}
	return nil
}

func (r *Reconciler) collectTargetReadyReports(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	jobName string,
	expectedRanks []int32,
) ([]targetready.RankReport, []string, error) {
	pods, err := r.targetValidationPods(ctx, migration.Namespace, jobName)
	if err != nil {
		return nil, nil, err
	}
	expected := make(map[int32]struct{}, len(expectedRanks))
	for _, rank := range expectedRanks {
		expected[rank] = struct{}{}
	}
	reports := make(map[int32]targetready.RankReport, len(expectedRanks))
	var issues []string
	for _, pod := range pods {
		if pod.Status.Phase != corev1.PodSucceeded {
			continue
		}
		rank, rankErr := targetReadyPodRank(pod, migration.Spec.Target.WorldSize)
		if rankErr != nil {
			issues = append(issues, fmt.Sprintf("pod %s: %v", pod.Name, rankErr))
			continue
		}
		if _, ok := expected[rank]; !ok {
			issues = append(issues, fmt.Sprintf(
				"pod %s reported unexpected rank %d",
				pod.Name,
				rank,
			))
			continue
		}
		var podReports []targetready.RankReport
		var podIssues []string
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
				podIssues = append(podIssues, fmt.Sprintf(
					"pod %s container %s: %v",
					pod.Name,
					status.Name,
					err,
				))
				continue
			}
			if report.MigrationUID != migrationUID(migration) {
				podIssues = append(podIssues, fmt.Sprintf(
					"pod %s container %s: migrationUID %q does not match",
					pod.Name,
					status.Name,
					report.MigrationUID,
				))
				continue
			}
			if report.Rank != rank {
				podIssues = append(podIssues, fmt.Sprintf(
					"pod %s container %s: report rank %d does not match pod rank %d",
					pod.Name,
					status.Name,
					report.Rank,
					rank,
				))
				continue
			}
			podReports = append(podReports, report)
		}
		if len(podIssues) > 0 {
			issues = append(issues, podIssues...)
			continue
		}
		if len(podReports) == 0 {
			issues = append(issues, fmt.Sprintf(
				"pod %s has no target-ready report",
				pod.Name,
			))
			continue
		}
		for _, report := range podReports[1:] {
			if podReports[0] != report {
				issues = append(issues, fmt.Sprintf(
					"pod %s has conflicting target-ready reports for rank %d",
					pod.Name,
					rank,
				))
				continue
			}
		}
		if previous, ok := reports[rank]; ok && previous != podReports[0] {
			issues = append(issues, fmt.Sprintf(
				"conflicting target-ready reports for rank %d",
				rank,
			))
			continue
		}
		reports[rank] = podReports[0]
	}
	result := make([]targetready.RankReport, 0, len(reports))
	for _, report := range reports {
		result = append(result, report)
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Rank < result[j].Rank
	})
	return result, issues, nil
}

func (r *Reconciler) targetValidationPods(
	ctx context.Context,
	namespace string,
	jobName string,
) ([]corev1.Pod, error) {
	selectors := []map[string]string{
		{"batch.kubernetes.io/job-name": jobName},
		{"job-name": jobName},
	}
	byName := make(map[string]corev1.Pod)
	for _, selector := range selectors {
		var pods corev1.PodList
		if err := r.List(
			ctx,
			&pods,
			client.InNamespace(namespace),
			client.MatchingLabels(selector),
		); err != nil {
			return nil, fmt.Errorf("list target validation pods: %w", err)
		}
		for _, pod := range pods.Items {
			if !ownedByJob(pod, jobName) && pod.Labels["job-name"] != jobName &&
				pod.Labels["batch.kubernetes.io/job-name"] != jobName {
				continue
			}
			byName[pod.Name] = pod
		}
	}
	result := make([]corev1.Pod, 0, len(byName))
	for _, pod := range byName {
		result = append(result, pod)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].CreationTimestamp.Equal(&result[j].CreationTimestamp) {
			return result[i].Name < result[j].Name
		}
		return result[i].CreationTimestamp.Before(&result[j].CreationTimestamp)
	})
	return result, nil
}

func ownedByJob(pod corev1.Pod, jobName string) bool {
	for _, owner := range pod.OwnerReferences {
		if owner.Kind == "Job" && owner.Name == jobName {
			return true
		}
	}
	return false
}

func targetReadyPodRank(pod corev1.Pod, worldSize int32) (int32, error) {
	value := ""
	if pod.Annotations != nil {
		value = pod.Annotations["batch.kubernetes.io/job-completion-index"]
	}
	if value == "" {
		if worldSize == 1 {
			return 0, nil
		}
		return -1, fmt.Errorf("missing job completion index")
	}
	rank, err := strconv.ParseInt(value, 10, 32)
	if err != nil || rank < 0 {
		return -1, fmt.Errorf("invalid job completion index %q", value)
	}
	return int32(rank), nil
}

func targetValidationJobFailed(job *batchv1.Job) (bool, string) {
	for _, condition := range job.Status.Conditions {
		if condition.Type == batchv1.JobFailed &&
			condition.Status == corev1.ConditionTrue {
			message := strings.TrimSpace(condition.Message)
			if message == "" {
				message = "target validation Job failed"
			}
			if condition.Reason != "" {
				message = condition.Reason + ": " + message
			}
			return true, message
		}
	}
	return false, ""
}

func targetValidationJobComplete(job *batchv1.Job) bool {
	for _, condition := range job.Status.Conditions {
		if condition.Type == batchv1.JobComplete &&
			condition.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

func targetValidationRanks(
	migration *aevrelv1.CheckpointMigration,
) ([]int32, error) {
	worldSize := migration.Spec.Target.WorldSize
	if worldSize <= 0 {
		return nil, fmt.Errorf("target worldSize must be positive")
	}
	if len(migration.Spec.Target.RankMap) == 0 {
		ranks := make([]int32, worldSize)
		for index := range ranks {
			ranks[index] = int32(index)
		}
		return ranks, nil
	}
	if len(migration.Spec.Target.RankMap) != int(worldSize) {
		return nil, fmt.Errorf(
			"target rankMap has %d ranks, want %d",
			len(migration.Spec.Target.RankMap),
			worldSize,
		)
	}
	ranks := make([]int32, 0, len(migration.Spec.Target.RankMap))
	seen := make(map[int32]struct{}, len(migration.Spec.Target.RankMap))
	for _, endpoint := range migration.Spec.Target.RankMap {
		if endpoint.Rank < 0 || endpoint.Rank >= worldSize {
			return nil, fmt.Errorf(
				"target rank %d is outside 0..%d",
				endpoint.Rank,
				worldSize-1,
			)
		}
		if _, ok := seen[endpoint.Rank]; ok {
			return nil, fmt.Errorf("target rankMap contains duplicate rank %d", endpoint.Rank)
		}
		seen[endpoint.Rank] = struct{}{}
		ranks = append(ranks, endpoint.Rank)
	}
	sort.Slice(ranks, func(i, j int) bool { return ranks[i] < ranks[j] })
	for index, rank := range ranks {
		if rank != int32(index) {
			return nil, fmt.Errorf(
				"target validation requires contiguous ranks 0..%d",
				worldSize-1,
			)
		}
	}
	return ranks, nil
}

func (r *Reconciler) finishTargetReady(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	settings targetValidationSettings,
	jobName string,
	startedAt string,
	condition string,
	message string,
	evaluation targetready.Evaluation,
) (ctrl.Result, error) {
	if condition == "Ready" && !evaluation.Ready {
		condition = "Incomplete"
		message = "target-ready evaluation did not pass"
	}
	if evaluation.Condition == "" {
		evaluation.Condition = condition
	}
	if evaluation.Message == "" {
		evaluation.Message = message
	}
	ready := condition == "Ready" && evaluation.Ready
	proofRef := ""
	proofHash := ""
	aggregate := targetready.NewAggregate(
		migrationUID(migration),
		settings.Version,
		jobName,
		startedAt,
		evaluation,
	)
	if start, err := time.Parse(time.RFC3339, startedAt); err == nil {
		aggregate.DurationMilliseconds = time.Since(start).Milliseconds()
	}
	payload, err := aggregate.Marshal()
	if err != nil {
		return ctrl.Result{}, err
	}
	if r.Artifacts == nil {
		condition = "ProofPublishFailed"
		ready = false
		message = "artifact store is not configured; target-ready proof cannot be published"
	} else {
		ref, err := r.Artifacts.Put(ctx, payload)
		if err != nil {
			condition = "ProofPublishFailed"
			ready = false
			message = "publish target-ready proof: " + err.Error()
		} else {
			proofRef = ref.URL
			proofHash = ref.Hash
		}
	}
	phase := aevrelv1.MigrationPhaseTargetFailed
	if ready {
		phase = aevrelv1.MigrationPhaseSucceeded
	}
	updated, err := r.updateTargetReadyStatus(
		ctx,
		migration,
		func(current *aevrelv1.CheckpointMigration) bool {
			before := current.Status.DeepCopy()
			now := time.Now().UTC().Format(time.RFC3339)
			target := ensureTargetReadyStatus(current)
			target.Condition = condition
			target.Ready = ready
			target.Version = settings.Version
			target.RanksReady = evaluation.RanksReady
			target.RanksTotal = evaluation.RanksTotal
			target.StepsRan = evaluation.StepsRan
			target.StateComplete = evaluation.StateComplete
			if start, err := time.Parse(time.RFC3339, startedAt); err == nil {
				target.DurationMilliseconds = time.Since(start).Milliseconds()
			}
			target.JobName = jobName
			target.ProofRef = proofRef
			target.ProofHash = proofHash
			target.Message = message
			target.StartedAt = startedAt
			target.CompletedAt = now
			target.Ranks = targetReadyRankStatuses(evaluation.Reports)
			current.Status.Phase = phase
			current.Status.CompletedAt = now
			if current.Spec.Mode == aevrelv1.MigrationModeLiveRankSplit {
				if current.Status.Live == nil {
					current.Status.Live = &aevrelv1.LiveRankSplitStatus{}
				}
				if ready {
					current.Status.Live.CutoverPhase = "succeeded"
				} else {
					current.Status.Live.CutoverPhase = "failed"
				}
				if current.Status.Live.Message == "" {
					current.Status.Live.Message = message
				}
				current.Status.Live.CompletedAt = now
			}
			conditionStatus := metav1.ConditionFalse
			if ready {
				conditionStatus = metav1.ConditionTrue
			}
			meta.SetStatusCondition(&current.Status.Conditions, metav1.Condition{
				Type:               targetReadyCondition,
				Status:             conditionStatus,
				Reason:             condition,
				Message:            message,
				ObservedGeneration: current.Generation,
			})
			return !reflect.DeepEqual(before, &current.Status)
		},
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if r.Recorder != nil {
		eventType := corev1.EventTypeWarning
		if ready {
			eventType = corev1.EventTypeNormal
		}
		r.Recorder.Event(
			updated,
			eventType,
			"TargetReady"+condition,
			message,
		)
	}
	return ctrl.Result{}, nil
}

func (r *Reconciler) updateTargetReadyStatus(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	mutate func(*aevrelv1.CheckpointMigration) bool,
) (*aevrelv1.CheckpointMigration, error) {
	key := types.NamespacedName{
		Namespace: migration.Namespace,
		Name:      migration.Name,
	}
	var result *aevrelv1.CheckpointMigration
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var latest aevrelv1.CheckpointMigration
		if err := r.Get(ctx, key, &latest); err != nil {
			return err
		}
		if !mutate(&latest) {
			result = latest.DeepCopy()
			return nil
		}
		if err := r.Status().Update(ctx, &latest); err != nil {
			return err
		}
		result = latest.DeepCopy()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

func ensureTargetReadyStatus(
	migration *aevrelv1.CheckpointMigration,
) *aevrelv1.TargetReadyStatus {
	if migration.Status.TargetReady == nil {
		migration.Status.TargetReady = &aevrelv1.TargetReadyStatus{}
	}
	return migration.Status.TargetReady
}

func targetReadyRankStatuses(
	reports []targetready.RankReport,
) []aevrelv1.TargetReadyRankStatus {
	result := make([]aevrelv1.TargetReadyRankStatus, 0, len(reports))
	for _, report := range reports {
		result = append(result, aevrelv1.TargetReadyRankStatus{
			Rank:            report.Rank,
			ModelLoaded:     report.ModelLoaded,
			OptimizerLoaded: report.OptimizerLoaded,
			StepsRan:        report.StepsRan,
			StateComplete:   report.StateComplete,
			OK:              report.OK,
			Error:           report.Error,
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Rank < result[j].Rank
	})
	return result
}
