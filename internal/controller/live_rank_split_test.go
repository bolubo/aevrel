package controller

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/targetready"
)

func TestValidateMigrationMode(t *testing.T) {
	tests := []struct {
		name    string
		mutate  func(*aevrelv1.CheckpointMigrationSpec)
		wantErr string
	}{
		{
			name: "static defaults when mode is empty",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Mode = ""
				spec.Live = nil
			},
		},
		{
			name: "accepts L3 without target validation",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.StateMode = aevrelv1.LiveStateModeL3
				spec.Live.Cutover.RequireStateComplete = boolPointer(false)
				spec.Target.Validation = nil
			},
		},
		{
			name: "accepts L4 with target validation",
		},
		{
			name: "rejects live block in static mode",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Mode = aevrelv1.MigrationModeStatic
			},
			wantErr: "live is only valid",
		},
		{
			name: "rejects live mode without live block",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live = nil
			},
			wantErr: "live is required",
		},
		{
			name: "rejects unknown mode",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Mode = "teleport"
			},
			wantErr: "unsupported migration mode",
		},
		{
			name: "rejects single-rank live split",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Source.WorldSize = 1
				spec.Target.WorldSize = 1
			},
			wantErr: "source.worldSize >= 2",
		},
		{
			name: "rejects world size change",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Target.WorldSize = 3
			},
			wantErr: "source.worldSize == target.worldSize",
		},
		{
			name: "rejects zero in-flight ranks",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.MaxInFlight = 0
			},
			wantErr: "live.maxInFlight",
		},
		{
			name: "rejects all ranks in flight",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.MaxInFlight = spec.Source.WorldSize
			},
			wantErr: "live.maxInFlight",
		},
		{
			name: "rejects invalid state mode",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.StateMode = "L2"
			},
			wantErr: "live.stateMode",
		},
		{
			name: "rejects empty state root",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.StateRoot = ""
			},
			wantErr: "live.stateRoot",
		},
		{
			name: "rejects empty state providers",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.StateProviders = nil
			},
			wantErr: "live.stateProviders",
		},
		{
			name: "rejects negative quorum",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.Cutover.Quorum = -1
			},
			wantErr: "quorum is out of range",
		},
		{
			name: "rejects quorum above world size",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.Cutover.Quorum = spec.Source.WorldSize + 1
			},
			wantErr: "quorum is out of range",
		},
		{
			name: "rejects minority quorum",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.Cutover.Quorum = 2
			},
			wantErr: "quorum must be at least",
		},
		{
			name: "L4 requires target validation",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Target.Validation = nil
			},
			wantErr: "requires target.validation",
		},
		{
			name: "rejects negative rank plan dimensions",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.RankPlan.TP = -1
			},
			wantErr: "tp/pp",
		},
		{
			name: "rejects duplicate rank plan rank",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.RankPlan.Ranks = []int32{0, 1, 1}
			},
			wantErr: "duplicated",
		},
		{
			name: "rejects out-of-range rank plan rank",
			mutate: func(spec *aevrelv1.CheckpointMigrationSpec) {
				spec.Live.RankPlan.Ranks = []int32{0, 4}
			},
			wantErr: "out of range",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			migration := validLiveMigration("mode")
			if test.mutate != nil {
				test.mutate(&migration.Spec)
			}
			err := validateMigrationMode(&migration.Spec)
			if test.wantErr == "" {
				if err != nil {
					t.Fatalf("validateMigrationMode() error = %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf(
					"validateMigrationMode() error = %v, want %q",
					err,
					test.wantErr,
				)
			}
		})
	}
}

func TestLiveRequiresState(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*aevrelv1.CheckpointMigration)
		want   bool
	}{
		{
			name: "static",
			mutate: func(migration *aevrelv1.CheckpointMigration) {
				migration.Spec.Mode = aevrelv1.MigrationModeStatic
			},
		},
		{
			name: "L3 does not require state",
			mutate: func(migration *aevrelv1.CheckpointMigration) {
				migration.Spec.Live.StateMode = aevrelv1.LiveStateModeL3
				migration.Spec.Live.Cutover.RequireStateComplete = boolPointer(false)
			},
		},
		{
			name: "L3 can opt into state",
			mutate: func(migration *aevrelv1.CheckpointMigration) {
				migration.Spec.Live.StateMode = aevrelv1.LiveStateModeL3
				migration.Spec.Live.Cutover.RequireStateComplete = boolPointer(true)
			},
			want: true,
		},
		{
			name: "L4 requires state",
			want: true,
		},
		{
			name: "empty state mode defaults to L4",
			mutate: func(migration *aevrelv1.CheckpointMigration) {
				migration.Spec.Live.StateMode = ""
			},
			want: true,
		},
		{
			name: "missing live block",
			mutate: func(migration *aevrelv1.CheckpointMigration) {
				migration.Spec.Live = nil
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			migration := validLiveMigration("requires-state")
			if test.mutate != nil {
				test.mutate(migration)
			}
			if got := liveRequiresState(migration); got != test.want {
				t.Fatalf("liveRequiresState() = %t, want %t", got, test.want)
			}
		})
	}
}

func TestLiveRankSplitFailsClosedWithoutExecutor(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validLiveMigration("missing-executor")
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointMigration{}).
		WithObjects(migration).
		Build()
	reconciler := &Reconciler{Client: kubeClient, Scheme: scheme}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointMigration
	if err := kubeClient.Get(context.Background(), request.NamespacedName, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Phase != aevrelv1.MigrationPhaseFailed {
		t.Fatalf("phase = %q, want failed", got.Status.Phase)
	}
	if got.Status.Live == nil ||
		got.Status.Live.CutoverPhase != "failed" ||
		!strings.Contains(got.Status.Live.Message, "not configured") {
		t.Fatalf("live status = %+v", got.Status.Live)
	}
}

func TestLiveRankSplitPersistsExecutorProgress(t *testing.T) {
	migration := validLiveMigration("progress")
	reconciler, kubeClient, request := liveReconciler(
		t,
		migration,
		&fakeLiveExecutor{observation: LiveRankSplitObservation{
			Phase:    "following",
			InFlight: []int32{1},
			Coverage: []int32{0, 1, 2, 3},
			Quorum:   3,
			Message:  "rank 1 is catching up",
		}},
	)
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTransferring {
		t.Fatalf("phase = %q, want transferring", got.Status.Phase)
	}
	if got.Status.Live == nil ||
		got.Status.Live.CutoverPhase != "following" ||
		len(got.Status.Live.InFlight) != 1 {
		t.Fatalf("live = %+v", got.Status.Live)
	}
}

func TestLiveRankSplitPromotesThroughTargetValidation(t *testing.T) {
	migration := validLiveMigration("target-gate")
	reconciler, kubeClient, request := liveReconciler(
		t,
		migration,
		&fakeLiveExecutor{observation: completedLiveObservation()},
	)
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTargetValidating {
		t.Fatalf("phase = %q, want target-validating", got.Status.Phase)
	}
	if got.Status.Live == nil ||
		got.Status.Live.CutoverPhase != "target-validating" ||
		!got.Status.Live.StateComplete ||
		got.Status.ProofRef == "" {
		t.Fatalf("live = %+v proofRef=%q", got.Status.Live, got.Status.ProofRef)
	}
}

func TestLiveRankSplitL3CanCompleteWithoutTargetValidation(t *testing.T) {
	migration := validLiveMigration("l3")
	migration.Spec.Live.StateMode = aevrelv1.LiveStateModeL3
	migration.Spec.Live.Cutover.RequireStateComplete = boolPointer(false)
	migration.Spec.Target.Validation = nil
	reconciler, kubeClient, request := liveReconciler(
		t,
		migration,
		&fakeLiveExecutor{observation: completedLiveObservation()},
	)
	if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseSucceeded {
		t.Fatalf("phase = %q, want succeeded", got.Status.Phase)
	}
}

func TestLiveRankSplitRejectsUnsafeCompletion(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*LiveRankSplitObservation)
	}{
		{
			name: "missing coverage",
			mutate: func(observation *LiveRankSplitObservation) {
				observation.Coverage = []int32{0, 1, 2}
			},
		},
		{
			name: "in-flight rank remains",
			mutate: func(observation *LiveRankSplitObservation) {
				observation.InFlight = []int32{3}
			},
		},
		{
			name: "quorum too small",
			mutate: func(observation *LiveRankSplitObservation) {
				observation.Quorum = 2
			},
		},
		{
			name: "state closure missing",
			mutate: func(observation *LiveRankSplitObservation) {
				observation.StateComplete = false
			},
		},
		{
			name: "unknown phase",
			mutate: func(observation *LiveRankSplitObservation) {
				observation.Phase = "vibing"
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			migration := validLiveMigration("unsafe")
			observation := completedLiveObservation()
			test.mutate(&observation)
			reconciler, kubeClient, request := liveReconciler(
				t,
				migration,
				&fakeLiveExecutor{observation: observation},
			)
			if _, err := reconciler.Reconcile(context.Background(), request); err != nil {
				t.Fatal(err)
			}
			got := getMigration(t, kubeClient, migration)
			if got.Status.Phase != aevrelv1.MigrationPhaseFailed {
				t.Fatalf("phase = %q, want failed", got.Status.Phase)
			}
		})
	}
}

func TestLiveRankSplitExecutorFailureIsRetryable(t *testing.T) {
	migration := validLiveMigration("retryable")
	reconciler, kubeClient, request := liveReconciler(
		t,
		migration,
		&fakeLiveExecutor{err: context.DeadlineExceeded},
	)
	result, err := reconciler.Reconcile(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if result.RequeueAfter != liveRankSplitPollInterval {
		t.Fatalf("requeue = %s", result.RequeueAfter)
	}
	got := getMigration(t, kubeClient, migration)
	if got.Status.Phase != aevrelv1.MigrationPhaseTransferring {
		t.Fatalf("phase = %q, want transferring", got.Status.Phase)
	}
	if got.Status.Live == nil ||
		got.Status.Live.CutoverPhase != "failed" {
		t.Fatalf("live = %+v", got.Status.Live)
	}
}

func TestFinishTargetReadyEnforcesL4StateClosure(t *testing.T) {
	for _, test := range []struct {
		name          string
		stateComplete bool
		wantPhase     string
		wantCondition string
		wantLivePhase string
	}{
		{
			name:          "incomplete state fails closed",
			stateComplete: false,
			wantPhase:     aevrelv1.MigrationPhaseTargetFailed,
			wantCondition: "StateIncomplete",
			wantLivePhase: "failed",
		},
		{
			name:          "complete state succeeds",
			stateComplete: true,
			wantPhase:     aevrelv1.MigrationPhaseSucceeded,
			wantCondition: "Ready",
			wantLivePhase: "succeeded",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			migration := validLiveMigration("l4-gate")
			migration.Status.Phase = aevrelv1.MigrationPhaseTargetValidating
			reconciler, kubeClient := targetReadyReconciler(t, migration)
			settings, enabled, err := parseTargetValidationSettings(migration)
			if err != nil || !enabled {
				t.Fatalf("settings = %+v enabled=%t err=%v", settings, enabled, err)
			}
			if _, err := reconciler.finishTargetReady(
				context.Background(),
				migration,
				settings,
				"target-validation",
				metav1.Now().UTC().Format(timeRFC3339),
				"Ready",
				"all target ranks are ready",
				targetready.Evaluation{
					Ready:         true,
					RanksReady:    4,
					RanksTotal:    4,
					StepsRan:      10,
					StateComplete: test.stateComplete,
				},
			); err != nil {
				t.Fatal(err)
			}
			got := getMigration(t, kubeClient, migration)
			if got.Status.Phase != test.wantPhase {
				t.Fatalf("phase = %q, want %q", got.Status.Phase, test.wantPhase)
			}
			if got.Status.TargetReady == nil ||
				got.Status.TargetReady.Condition != test.wantCondition {
				t.Fatalf("targetReady = %+v", got.Status.TargetReady)
			}
			if got.Status.Live == nil ||
				got.Status.Live.CutoverPhase != test.wantLivePhase ||
				got.Status.Live.CompletedAt == "" {
				t.Fatalf("live = %+v", got.Status.Live)
			}
		})
	}
}

func TestRenderTargetValidationJobInjectsL4StateContract(t *testing.T) {
	migration := validLiveMigration("l4-job")
	migration.Spec.Target.Validation.JobName = ""
	migration.Spec.Target.Validation.JobTemplate = validationJobTemplate(t, 4)
	settings, enabled, err := parseTargetValidationSettings(migration)
	if err != nil || !enabled {
		t.Fatalf("settings = %+v enabled=%t err=%v", settings, enabled, err)
	}
	job, err := renderTargetValidationJob(migration, settings)
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(job.Spec.Template.Spec.Containers[0].Env)
	if env["AEVREL_STATE_TEMPLATE"] != "/state/step-100/rank{rank}" {
		t.Fatalf("state template = %q", env["AEVREL_STATE_TEMPLATE"])
	}
	if env["AEVREL_STATE_PROVIDERS"] != "python.random,numpy.random" {
		t.Fatalf("state providers = %q", env["AEVREL_STATE_PROVIDERS"])
	}
	if env["AEVREL_REQUIRE_STATE"] != "1" {
		t.Fatalf("require state = %q", env["AEVREL_REQUIRE_STATE"])
	}
}

const timeRFC3339 = "2006-01-02T15:04:05Z07:00"

func validLiveMigration(name string) *aevrelv1.CheckpointMigration {
	migration := validMigration(name)
	migration.Spec.Mode = aevrelv1.MigrationModeLiveRankSplit
	migration.Spec.Source.WorldSize = 4
	migration.Spec.Source.Mesh.Shape = []int32{4}
	migration.Spec.Source.RankMap = []aevrelv1.RankEndpoint{
		{Rank: 0, Endpoint: "https://source-0:9443"},
		{Rank: 1, Endpoint: "https://source-1:9443"},
		{Rank: 2, Endpoint: "https://source-2:9443"},
		{Rank: 3, Endpoint: "https://source-3:9443"},
	}
	migration.Spec.Target.WorldSize = 4
	migration.Spec.Target.Mesh.Shape = []int32{4}
	migration.Spec.Target.RankMap = []aevrelv1.RankEndpoint{
		{Rank: 0, Endpoint: "https://target-0:9443"},
		{Rank: 1, Endpoint: "https://target-1:9443"},
		{Rank: 2, Endpoint: "https://target-2:9443"},
		{Rank: 3, Endpoint: "https://target-3:9443"},
	}
	migration.Spec.Target.Validation = &aevrelv1.TargetValidationSpec{
		Version: "step-100",
		Steps:   10,
		JobName: "target-validation",
	}
	migration.Spec.Live = &aevrelv1.LiveRankSplitSpec{
		MaxInFlight:    1,
		StateMode:      aevrelv1.LiveStateModeL4,
		StateRoot:      "/state/step-100/rank{rank}",
		StateProviders: []string{"python.random", "numpy.random"},
		Cutover: aevrelv1.LiveCutoverSpec{
			Quorum:               3,
			RequireStateComplete: boolPointer(true),
		},
		RankPlan: &aevrelv1.LiveRankPlanSpec{
			TP:    2,
			PP:    2,
			Ranks: []int32{0, 1, 2, 3},
		},
	}
	return migration
}

type fakeLiveExecutor struct {
	observation LiveRankSplitObservation
	err         error
}

func (f *fakeLiveExecutor) Observe(
	context.Context,
	*aevrelv1.CheckpointMigration,
) (LiveRankSplitObservation, error) {
	return f.observation, f.err
}

func liveReconciler(
	t *testing.T,
	migration *aevrelv1.CheckpointMigration,
	live LiveRankSplitExecutor,
) (*Reconciler, client.Client, ctrl.Request) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
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
		Live:   live,
	}
	request := ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: migration.Namespace,
			Name:      migration.Name,
		},
	}
	return reconciler, kubeClient, request
}

func completedLiveObservation() LiveRankSplitObservation {
	return LiveRankSplitObservation{
		Phase:         "succeeded",
		InFlight:      []int32{},
		Coverage:      []int32{0, 1, 2, 3},
		Quorum:        3,
		StateComplete: true,
		StateRoot:     "/state/step-100/rank{rank}",
		ProofRef:      "https://artifacts/live-proof.json",
		Message:       "all ranks joined",
	}
}

func boolPointer(value bool) *bool {
	return &value
}
