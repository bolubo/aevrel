package controller

import (
	"context"
	"encoding/json"
	"strconv"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/artifactstore"
	"github.com/bolubo/aevrel/internal/targetready"
)

func TestRenderTargetValidationJobInjectsContract(t *testing.T) {
	migration := targetValidationMigration("render")
	migration.Spec.Target.Validation = &aevrelv1.TargetValidationSpec{
		Version:        "step-100",
		Steps:          12,
		TimeoutSeconds: 600,
		JobTemplate:    validationJobTemplate(t, 2),
	}
	settings, enabled, err := parseTargetValidationSettings(migration)
	if err != nil || !enabled {
		t.Fatalf("settings = %+v, enabled=%v, err=%v", settings, enabled, err)
	}
	job, err := renderTargetValidationJob(migration, settings)
	if err != nil {
		t.Fatal(err)
	}
	if job.Spec.CompletionMode == nil ||
		*job.Spec.CompletionMode != batchv1.IndexedCompletion {
		t.Fatalf("completionMode = %v", job.Spec.CompletionMode)
	}
	if job.Spec.Completions == nil || *job.Spec.Completions != 2 {
		t.Fatalf("completions = %v", job.Spec.Completions)
	}
	container := job.Spec.Template.Spec.Containers[0]
	if container.TerminationMessagePath != targetReadyReportPath {
		t.Fatalf("terminationMessagePath = %q", container.TerminationMessagePath)
	}
	env := envMap(container.Env)
	if env["AEVREL_VERSION"] != "step-100" {
		t.Fatalf("version env = %q", env["AEVREL_VERSION"])
	}
	if env["AEVREL_VALIDATION_STEPS"] != "12" {
		t.Fatalf("steps env = %q", env["AEVREL_VALIDATION_STEPS"])
	}
	if env["AEVREL_REPORT_PATH"] != targetReadyReportPath {
		t.Fatalf("report env = %q", env["AEVREL_REPORT_PATH"])
	}
	if len(job.OwnerReferences) != 0 {
		t.Fatalf("unexpected owner references without a UID: %+v", job.OwnerReferences)
	}
}

func TestReconcileTargetReadySuccess(t *testing.T) {
	migration := targetValidationMigration("target-ready-success")
	job := completedValidationJob(migration, "validate-target")
	objects := []client.Object{migration, job}
	for rank := int32(0); rank < 2; rank++ {
		objects = append(objects, validationPod(migration, job.Name, rank, reportFor(migration, rank, true)))
	}
	reconciler, kubeClient := targetReadyReconciler(t, objects...)

	if _, err := reconciler.reconcileTargetReady(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseSucceeded {
		t.Fatalf("phase = %q, want succeeded", got.Status.Phase)
	}
	if got.Status.TargetReady == nil || !got.Status.TargetReady.Ready {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
	if got.Status.TargetReady.RanksReady != 2 ||
		got.Status.TargetReady.ProofRef == "" {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, targetReadyCondition)
	if condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("TargetReady condition = %+v", condition)
	}
}

func TestReconcileTargetReadyFailsClosedOnMissingRank(t *testing.T) {
	migration := targetValidationMigration("target-ready-missing")
	job := completedValidationJob(migration, "validate-target")
	objects := []client.Object{
		migration,
		job,
		validationPod(migration, job.Name, 0, reportFor(migration, 0, true)),
	}
	reconciler, kubeClient := targetReadyReconciler(t, objects...)

	if _, err := reconciler.reconcileTargetReady(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTargetFailed {
		t.Fatalf("phase = %q, want target-failed", got.Status.Phase)
	}
	if got.Status.TargetReady == nil ||
		got.Status.TargetReady.Condition != "Incomplete" ||
		got.Status.TargetReady.RanksReady != 1 {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, targetReadyCondition)
	if condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("TargetReady condition = %+v", condition)
	}
}

func TestReconcileTargetReadyFailsClosedOnJobFailure(t *testing.T) {
	migration := targetValidationMigration("target-ready-job-failed")
	job := completedValidationJob(migration, "validate-target")
	job.Status.Conditions = []batchv1.JobCondition{{
		Type:    batchv1.JobFailed,
		Status:  corev1.ConditionTrue,
		Reason:  "BackoffLimitExceeded",
		Message: "target training failed",
	}}
	reconciler, kubeClient := targetReadyReconciler(t, migration, job)

	if _, err := reconciler.reconcileTargetReady(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTargetFailed {
		t.Fatalf("phase = %q, want target-failed", got.Status.Phase)
	}
	if got.Status.TargetReady == nil ||
		got.Status.TargetReady.Condition != "JobFailed" {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
}

func TestReconcileTargetReadySurvivesRestart(t *testing.T) {
	migration := targetValidationMigration("target-ready-restart")
	job := completedValidationJob(migration, "validate-target")
	job.Status = batchv1.JobStatus{}
	reconciler, kubeClient := targetReadyReconciler(t, migration, job)

	if _, err := reconciler.reconcileTargetReady(context.Background(), migration); err != nil {
		t.Fatal(err)
	}
	first := getMigration(t, kubeClient, migration)
	if first.Status.Phase != aevrelv1.MigrationPhaseTargetValidating {
		t.Fatalf("first phase = %q", first.Status.Phase)
	}

	var currentJob batchv1.Job
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKeyFromObject(job),
		&currentJob,
	); err != nil {
		t.Fatal(err)
	}
	currentJob.Status.Conditions = []batchv1.JobCondition{{
		Type:   batchv1.JobComplete,
		Status: corev1.ConditionTrue,
	}}
	if err := kubeClient.Status().Update(context.Background(), &currentJob); err != nil {
		t.Fatal(err)
	}
	for rank := int32(0); rank < 2; rank++ {
		if err := kubeClient.Create(
			context.Background(),
			validationPod(migration, job.Name, rank, reportFor(migration, rank, true)),
		); err != nil {
			t.Fatal(err)
		}
	}

	// A fresh reconciler has no in-memory state and must recover from the
	// completed Job and Pod termination messages.
	restarted := &Reconciler{
		Client:    kubeClient,
		Artifacts: artifactstore.NewMemoryStore("http://controller:8082"),
	}
	if _, err := restarted.reconcileTargetReady(context.Background(), &first); err != nil {
		t.Fatal(err)
	}
	second := getMigration(t, kubeClient, migration)
	if second.Status.Phase != aevrelv1.MigrationPhaseSucceeded {
		t.Fatalf("second phase = %q", second.Status.Phase)
	}
}

func TestReconcileCommitStartsTargetValidation(t *testing.T) {
	migration := validMigration("commit-target-ready")
	migration.Spec.Target.Validation = &aevrelv1.TargetValidationSpec{
		Version:        "step-100",
		Steps:          10,
		TimeoutSeconds: 600,
		JobTemplate:    validationJobTemplate(t, 2),
	}
	agents := &fakeAgentAPI{
		inventories: map[int32]agentclient.Inventory{
			0: fakeInventory(0),
			1: fakeInventory(1),
		},
		plan: map[string]any{"version": "1", "nodes": []any{}},
	}
	tasks := &fakeTaskAPI{
		tasks:    map[string]agentclient.Task{},
		failRank: -1,
	}
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := batchv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{
		Client: kubeClient,
		Scheme: scheme,
		Orchestrator: &Orchestrator{
			Agents:    agents,
			Tasks:     tasks,
			Artifacts: artifactstore.NewMemoryStore("http://controller:8082"),
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: client.ObjectKeyFromObject(migration),
	}); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTargetValidating {
		t.Fatalf("phase = %q, want target-validating", got.Status.Phase)
	}
	if got.Status.TargetReady == nil ||
		got.Status.TargetReady.Condition != "Pending" {
		t.Fatalf("targetReady = %+v", got.Status.TargetReady)
	}
	var job batchv1.Job
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKey{
			Namespace: migration.Namespace,
			Name:      targetValidationJobName(migration),
		},
		&job,
	); err != nil {
		t.Fatalf("target validation Job was not created: %v", err)
	}
}

func TestCancelDuringTargetValidationIsRejected(t *testing.T) {
	migration := targetValidationMigration("cancel-target-validation")
	migration.Annotations = map[string]string{
		"aevrel.dev/cancel": "true",
	}
	reconciler, kubeClient := targetReadyReconciler(t, migration)
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: client.ObjectKeyFromObject(migration),
	}); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTargetValidating {
		t.Fatalf("phase = %q, want target-validating", got.Status.Phase)
	}
	condition := meta.FindStatusCondition(got.Status.Conditions, "Accepted")
	if condition == nil || condition.Reason != "CancelRejected" {
		t.Fatalf("Accepted condition = %+v", condition)
	}
}

func targetReadyReconciler(
	t *testing.T,
	objects ...client.Object,
) (*Reconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := batchv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(
			&aevrelv1.CheckpointMigration{},
			&batchv1.Job{},
		)
	kubeClient = kubeClient.WithObjects(objects...)
	built := kubeClient.Build()
	return &Reconciler{
		Client:    built,
		Scheme:    scheme,
		Artifacts: artifactstore.NewMemoryStore("http://controller:8082"),
	}, built
}

func getMigration(
	t *testing.T,
	kubeClient client.Client,
	migration *aevrelv1.CheckpointMigration,
) aevrelv1.CheckpointMigration {
	t.Helper()
	var got aevrelv1.CheckpointMigration
	if err := kubeClient.Get(
		context.Background(),
		client.ObjectKeyFromObject(migration),
		&got,
	); err != nil {
		t.Fatal(err)
	}
	return got
}

func targetValidationMigration(name string) *aevrelv1.CheckpointMigration {
	migration := validMigration(name)
	migration.Status.Phase = aevrelv1.MigrationPhaseTargetValidating
	migration.Spec.Target.Validation = &aevrelv1.TargetValidationSpec{
		Version:        "step-100",
		Steps:          10,
		TimeoutSeconds: 600,
		JobName:        "validate-target",
	}
	return migration
}

func validationJobTemplate(t *testing.T, worldSize int32) *runtime.RawExtension {
	t.Helper()
	job := batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		Spec: batchv1.JobSpec{
			Template: corev1.PodTemplateSpec{
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{{
						Name:    "trainer",
						Image:   "example/trainer:test",
						Command: []string{"/bin/true"},
					}},
				},
			},
		},
	}
	payload, err := json.Marshal(job)
	if err != nil {
		t.Fatal(err)
	}
	_ = worldSize
	return &runtime.RawExtension{Raw: payload}
}

func completedValidationJob(
	migration *aevrelv1.CheckpointMigration,
	name string,
) *batchv1.Job {
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: migration.Namespace,
			Name:      name,
			Annotations: map[string]string{
				"aevrel.dev/migration-uid":  migrationUID(migration),
				"aevrel.dev/target-version": "step-100",
			},
		},
		Status: batchv1.JobStatus{
			Conditions: []batchv1.JobCondition{{
				Type:   batchv1.JobComplete,
				Status: corev1.ConditionTrue,
			}},
		},
	}
}

func validationPod(
	migration *aevrelv1.CheckpointMigration,
	jobName string,
	rank int32,
	report targetready.RankReport,
) *corev1.Pod {
	payload, _ := json.Marshal(report)
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: migration.Namespace,
			Name:      jobName + "-" + strconv.Itoa(int(rank)),
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

func reportFor(
	migration *aevrelv1.CheckpointMigration,
	rank int32,
	ok bool,
) targetready.RankReport {
	return targetready.RankReport{
		SchemaVersion:   targetready.SchemaVersion,
		MigrationUID:    migrationUID(migration),
		Version:         "step-100",
		Rank:            rank,
		Role:            "worker",
		ModelLoaded:     ok,
		OptimizerLoaded: ok,
		StepsRan:        10,
		StateComplete:   false,
		OK:              ok,
	}
}

func envMap(values []corev1.EnvVar) map[string]string {
	result := make(map[string]string, len(values))
	for _, value := range values {
		result[value.Name] = value.Value
	}
	return result
}
