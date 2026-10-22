package controller

import (
	"context"
	"encoding/json"
	"strconv"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/targetready"
)

func TestRestoreDrillReconcilerPromotesReadyRecord(t *testing.T) {
	report, err := json.Marshal(map[string]any{
		"schemaVersion":   "1",
		"migrationUID":    "drill-1",
		"version":         "step-100",
		"rank":            0,
		"role":            "worker",
		"modelLoaded":     true,
		"optimizerLoaded": true,
		"stepsRan":        10,
		"stateComplete":   true,
		"ok":              true,
		"error":           "",
	})
	if err != nil {
		t.Fatal(err)
	}
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Status: shardferryv1.CheckpointRecordStatus{
			MetadataDigest: "sha256:test",
		},
	}
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: "drill-job", Namespace: "default"},
		Status: batchv1.JobStatus{Conditions: []batchv1.JobCondition{{
			Type:   batchv1.JobComplete,
			Status: corev1.ConditionTrue,
		}}},
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "drill-job-0",
			Namespace: "default",
			Labels: map[string]string{
				"batch.kubernetes.io/job-name": "drill-job",
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodSucceeded,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "trainer",
				State: corev1.ContainerState{
					Terminated: &corev1.ContainerStateTerminated{
						Message: string(report),
					},
				},
			}},
		},
	}
	drill := &shardferryv1.RestoreDrill{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "drill",
			Namespace: "default",
			UID:       types.UID("drill-1"),
		},
		Spec: shardferryv1.RestoreDrillSpec{
			RecordRef:       "job42",
			JobName:         "drill-job",
			Version:         "step-100",
			TargetWorldSize: 1,
			Steps:           10,
		},
	}
	kubeClient := restoreDrillClient(t, record, job, pod, drill)
	reconciler := &RestoreDrillReconciler{
		Client: kubeClient,
		Scheme: kubeClient.Scheme(),
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "drill",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.RestoreDrill
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "drill"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != "Succeeded" {
		t.Fatalf("drill phase = %q, want Succeeded", got.Status.Phase)
	}
	if got.Status.StepsRan != 10 || got.Status.RanksReady != 1 {
		t.Fatalf("drill status = %+v", got.Status)
	}
	var updated shardferryv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&updated,
	); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Readiness.Loadable == nil ||
		!*updated.Status.Readiness.Loadable {
		t.Fatalf("record loadable = %v", updated.Status.Readiness.Loadable)
	}
	if updated.Status.Readiness.LastDrill != "default/drill" {
		t.Fatalf("record last drill = %q", updated.Status.Readiness.LastDrill)
	}
	if updated.Status.Readiness.LastDrillVersion != "step-100" ||
		updated.Status.Readiness.LastDrillDigest != "sha256:test" {
		t.Fatalf("record drill evidence = %+v", updated.Status.Readiness)
	}
	if got.Status.JobName != "drill-job" ||
		got.Status.StartedAt == "" ||
		got.Status.CompletedAt == "" {
		t.Fatalf("drill job status = %+v", got.Status)
	}
}

func TestRestoreDrillReconcilerCreatesIndexedJob(t *testing.T) {
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: shardferryv1.CheckpointRecordSpec{
			Checkpoint: "/checkpoints/step-100",
		},
	}
	drill := &shardferryv1.RestoreDrill{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "drill",
			Namespace: "default",
			UID:       types.UID("drill-uid"),
		},
		Spec: shardferryv1.RestoreDrillSpec{
			RecordRef:            "job42",
			JobTemplate:          validationJobTemplate(t, 2),
			Version:              "step-100",
			TargetWorldSize:      2,
			Steps:                10,
			CheckpointTemplate:   "/target/step-100/rank{rank}",
			StateTemplate:        "/state/step-100/rank{rank}",
			StateProviders:       []string{"python.random", "numpy.random"},
			RequireStateComplete: true,
		},
	}
	kubeClient := restoreDrillClient(t, record, drill)
	reconciler := &RestoreDrillReconciler{
		Client: kubeClient,
		Scheme: kubeClient.Scheme(),
	}
	result, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "drill",
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter != restoreDrillPollInterval {
		t.Fatalf("requeueAfter = %s", result.RequeueAfter)
	}

	var job batchv1.Job
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKey{Namespace: "default", Name: "drill-restore-drill"},
		&job,
	); err != nil {
		t.Fatal(err)
	}
	if job.Spec.CompletionMode == nil ||
		*job.Spec.CompletionMode != batchv1.IndexedCompletion {
		t.Fatalf("completionMode = %v", job.Spec.CompletionMode)
	}
	if job.Spec.Completions == nil || *job.Spec.Completions != 2 {
		t.Fatalf("completions = %v", job.Spec.Completions)
	}
	if job.Spec.Parallelism == nil || *job.Spec.Parallelism != 2 {
		t.Fatalf("parallelism = %v", job.Spec.Parallelism)
	}
	if job.Spec.Template.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Fatalf("restartPolicy = %q", job.Spec.Template.Spec.RestartPolicy)
	}
	if len(job.OwnerReferences) != 1 ||
		job.OwnerReferences[0].Kind != "RestoreDrill" ||
		job.OwnerReferences[0].UID != drill.UID {
		t.Fatalf("ownerReferences = %+v", job.OwnerReferences)
	}
	container := job.Spec.Template.Spec.Containers[0]
	if container.TerminationMessagePath != targetReadyReportPath ||
		container.TerminationMessagePolicy != corev1.TerminationMessageReadFile {
		t.Fatalf(
			"termination contract path=%q policy=%q",
			container.TerminationMessagePath,
			container.TerminationMessagePolicy,
		)
	}
	env := envMap(container.Env)
	if env["SHARDFERRY_MIGRATION_UID"] != "drill-uid" ||
		env["SHARDFERRY_VERSION"] != "step-100" ||
		env["SHARDFERRY_TARGET_WORLD_SIZE"] != "2" ||
		env["SHARDFERRY_VALIDATION_STEPS"] != "10" {
		t.Fatalf("target env = %+v", env)
	}
	if env["SHARDFERRY_CHECKPOINT_TEMPLATE"] != "/target/step-100/rank{rank}" ||
		env["SHARDFERRY_STATE_TEMPLATE"] != "/state/step-100/rank{rank}" ||
		env["SHARDFERRY_STATE_PROVIDERS"] != "python.random,numpy.random" ||
		env["SHARDFERRY_REQUIRE_STATE"] != "1" {
		t.Fatalf("restore env = %+v", env)
	}

	var got shardferryv1.RestoreDrill
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "drill"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != "Running" ||
		got.Status.JobName != "drill-restore-drill" ||
		got.Status.StartedAt == "" {
		t.Fatalf("drill status = %+v", got.Status)
	}
}

func TestRestoreDrillReconcilerRejectsInvalidJobTemplate(t *testing.T) {
	template := validationJobTemplate(t, 2)
	var job batchv1.Job
	if err := json.Unmarshal(template.Raw, &job); err != nil {
		t.Fatal(err)
	}
	nonIndexed := batchv1.NonIndexedCompletion
	job.Spec.CompletionMode = &nonIndexed
	payload, err := json.Marshal(job)
	if err != nil {
		t.Fatal(err)
	}
	template.Raw = payload

	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
	}
	drill := validManagedRestoreDrill(t, "drill-invalid", template)
	kubeClient := restoreDrillClient(t, record, drill)
	reconciler := &RestoreDrillReconciler{
		Client: kubeClient,
		Scheme: kubeClient.Scheme(),
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "drill-invalid",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.RestoreDrill
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "drill-invalid"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != "Failed" ||
		got.Status.Conditions[0].Reason != "InvalidJobTemplate" {
		t.Fatalf("drill status = %+v", got.Status)
	}
	var jobs batchv1.JobList
	if err := kubeClient.List(context.Background(), &jobs); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 0 {
		t.Fatalf("created Jobs = %d, want 0", len(jobs.Items))
	}
}

func TestRestoreDrillReconcilerFailsClosedOnJobFailure(t *testing.T) {
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
	}
	drill := observedRestoreDrill("drill-failed", "drill-job", 1)
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: "drill-job", Namespace: "default"},
		Status: batchv1.JobStatus{Conditions: []batchv1.JobCondition{{
			Type:    batchv1.JobFailed,
			Status:  corev1.ConditionTrue,
			Reason:  "BackoffLimitExceeded",
			Message: "target process exited",
		}}},
	}
	kubeClient := restoreDrillClient(t, record, drill, job)
	reconciler := &RestoreDrillReconciler{
		Client: kubeClient,
		Scheme: kubeClient.Scheme(),
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "drill-failed",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.RestoreDrill
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "drill-failed"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != "Failed" ||
		got.Status.Message != "BackoffLimitExceeded: target process exited" {
		t.Fatalf("drill status = %+v", got.Status)
	}
	var updated shardferryv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&updated,
	); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Readiness.Loadable == nil ||
		*updated.Status.Readiness.Loadable {
		t.Fatalf("record loadable = %v", updated.Status.Readiness.Loadable)
	}
}

func TestRestoreDrillReconcilerFailsClosedOnMissingRank(t *testing.T) {
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
	}
	drill := observedRestoreDrill("drill-missing", "drill-job", 2)
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: "drill-job", Namespace: "default"},
		Status: batchv1.JobStatus{Conditions: []batchv1.JobCondition{{
			Type:   batchv1.JobComplete,
			Status: corev1.ConditionTrue,
		}}},
	}
	pod := restoreDrillPod(t, "drill-job", 0, restoreDrillReport(t, "drill-uid", 0))
	kubeClient := restoreDrillClient(t, record, drill, job, pod)
	reconciler := &RestoreDrillReconciler{
		Client: kubeClient,
		Scheme: kubeClient.Scheme(),
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "drill-missing",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.RestoreDrill
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "drill-missing"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != "Failed" ||
		len(got.Status.MissingRanks) != 1 ||
		got.Status.MissingRanks[0] != 1 {
		t.Fatalf("drill status = %+v", got.Status)
	}
	var updated shardferryv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&updated,
	); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Readiness.Loadable == nil ||
		*updated.Status.Readiness.Loadable {
		t.Fatalf("record loadable = %v", updated.Status.Readiness.Loadable)
	}
}

func restoreDrillClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := batchv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(
			&shardferryv1.CheckpointRecord{},
			&shardferryv1.RestoreDrill{},
		).
		WithObjects(objects...).
		Build()
}

func validManagedRestoreDrill(
	t *testing.T,
	name string,
	template *runtime.RawExtension,
) *shardferryv1.RestoreDrill {
	t.Helper()
	return &shardferryv1.RestoreDrill{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
			UID:       types.UID("drill-uid"),
		},
		Spec: shardferryv1.RestoreDrillSpec{
			RecordRef:       "job42",
			JobTemplate:     template,
			Version:         "step-100",
			TargetWorldSize: 2,
			Steps:           10,
		},
	}
}

func observedRestoreDrill(
	name string,
	jobName string,
	worldSize int32,
) *shardferryv1.RestoreDrill {
	return &shardferryv1.RestoreDrill{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
			UID:       types.UID("drill-uid"),
		},
		Spec: shardferryv1.RestoreDrillSpec{
			RecordRef:       "job42",
			JobName:         jobName,
			Version:         "step-100",
			TargetWorldSize: worldSize,
			Steps:           10,
		},
	}
}

func restoreDrillReport(
	t *testing.T,
	uid string,
	rank int32,
) targetready.RankReport {
	t.Helper()
	return targetready.RankReport{
		SchemaVersion:   targetready.SchemaVersion,
		MigrationUID:    uid,
		Version:         "step-100",
		Rank:            rank,
		Role:            "worker",
		ModelLoaded:     true,
		OptimizerLoaded: true,
		StepsRan:        10,
		StateComplete:   true,
		OK:              true,
	}
}

func restoreDrillPod(
	t *testing.T,
	jobName string,
	rank int32,
	report targetready.RankReport,
) *corev1.Pod {
	t.Helper()
	payload, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      jobName + "-" + strconv.Itoa(int(rank)),
			Namespace: "default",
			Labels: map[string]string{
				"batch.kubernetes.io/job-name": jobName,
			},
			Annotations: map[string]string{
				"batch.kubernetes.io/job-completion-index": strconv.Itoa(int(rank)),
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodSucceeded,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "trainer",
				State: corev1.ContainerState{
					Terminated: &corev1.ContainerStateTerminated{
						Message: string(payload),
					},
				},
			}},
		},
	}
}
