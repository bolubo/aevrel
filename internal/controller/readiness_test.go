package controller

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

func TestCheckpointReadinessReconcilerMarksUnverified(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "job42",
			Namespace: "default",
		},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 4,
		},
		Status: aevrelv1.CheckpointRecordStatus{
			Complete:    true,
			LatestMtime: time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Ranks: []aevrelv1.CheckpointRankStatus{{
				Rank:  0,
				Bytes: 16,
			}},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	_, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{
			NamespacedName: types.NamespacedName{
				Namespace: "default",
				Name:      "job42",
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Readiness.Status != aevrelv1.ReadinessUnverified {
		t.Fatalf("readiness = %q, want %q", got.Status.Readiness.Status, aevrelv1.ReadinessUnverified)
	}
	if got.Status.Complete != true {
		t.Fatal("complete was not copied")
	}
}

func TestCheckpointReadinessReconcilerPreservesMatchingDrillEvidence(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	loadable := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 1,
		},
		Status: aevrelv1.CheckpointRecordStatus{
			MetadataDigest: "sha256:test",
			LatestMtime:    time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Complete:       true,
			Ranks: []aevrelv1.CheckpointRankStatus{{
				Rank:  0,
				Bytes: 16,
			}},
			Readiness: aevrelv1.CheckpointReadinessStatus{
				Loadable:               &loadable,
				LastDrill:              "default/drill",
				LastDrillVersion:       "step-100",
				LastDrillDigest:        "sha256:test",
				LastDrillSteps:         3,
				LastDrillRanksReported: []int32{0},
				LastDrillRanksExpected: 1,
			},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "job42",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Readiness.Status != aevrelv1.ReadinessReady {
		t.Fatalf("readiness = %+v", got.Status.Readiness)
	}
	if got.Status.Readiness.LastDrillVersion != "step-100" ||
		got.Status.Readiness.LastDrillDigest != "sha256:test" {
		t.Fatalf("drill evidence = %+v", got.Status.Readiness)
	}
	// The step and rank evidence decides whether the drill counts at all, so
	// it has to survive the round trip through the API. The rank identities
	// travel, not a count: a verdict has to be able to name the missing ranks.
	if got.Status.Readiness.LastDrillSteps != 3 ||
		len(got.Status.Readiness.LastDrillRanksReported) != 1 ||
		got.Status.Readiness.LastDrillRanksReported[0] != 0 ||
		got.Status.Readiness.LastDrillRanksExpected != 1 {
		t.Fatalf("drill coverage = %+v", got.Status.Readiness)
	}
}

// A drill that only claims success is not evidence. Without recorded step and
// rank coverage the assessment withdraws loadable instead of keeping it, and
// the controller must persist that withdrawal.
func TestCheckpointReadinessReconcilerWithdrawsUnprovenDrill(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	loadable := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 1,
		},
		Status: aevrelv1.CheckpointRecordStatus{
			MetadataDigest: "sha256:test",
			LatestMtime:    time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Complete:       true,
			Ranks: []aevrelv1.CheckpointRankStatus{{
				Rank:  0,
				Bytes: 16,
			}},
			Readiness: aevrelv1.CheckpointReadinessStatus{
				Loadable:         &loadable,
				LastDrill:        "default/drill",
				LastDrillVersion: "step-100",
				LastDrillDigest:  "sha256:test",
			},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{Client: kubeClient, Scheme: scheme}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "job42",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Readiness.Status != aevrelv1.ReadinessTargetBlocked {
		t.Fatalf("readiness = %+v", got.Status.Readiness)
	}
	if got.Status.Readiness.Loadable == nil || *got.Status.Readiness.Loadable {
		t.Fatalf("loadable should have been withdrawn: %+v", got.Status.Readiness)
	}
	codes := make([]string, 0, len(got.Status.Readiness.Reasons))
	for _, reason := range got.Status.Readiness.Reasons {
		codes = append(codes, reason.Code)
	}
	if !contains(codes, control.ReasonDrillStepsMissing) {
		t.Fatalf("reasons = %v", codes)
	}
}

func TestCheckpointReadinessReconcilerInvalidatesStaleDrill(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	loadable := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 1,
		},
		Status: aevrelv1.CheckpointRecordStatus{
			MetadataDigest: "sha256:new",
			LatestMtime:    time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Complete:       true,
			Readiness: aevrelv1.CheckpointReadinessStatus{
				Loadable:         &loadable,
				LastDrill:        "default/drill",
				LastDrillVersion: "step-99",
				LastDrillDigest:  "sha256:old",
			},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client: kubeClient,
		Scheme: scheme,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "job42",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.Readiness.Loadable == nil ||
		*got.Status.Readiness.Loadable ||
		got.Status.Readiness.Status != aevrelv1.ReadinessTargetBlocked {
		t.Fatalf("readiness = %+v", got.Status.Readiness)
	}
}

func TestCheckpointReadinessReconcilerDiscoversAgent(t *testing.T) {
	agent := httptest.NewUnstartedServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.URL.Path != "/v1/checkpoint-record" {
			http.NotFound(writer, request)
			return
		}
		if request.Header.Get("Authorization") != "Bearer secret" {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		fmt.Fprint(writer, `{
			"version": "2",
			"resumeState": {
				"manifestDigest": "sha256:`+strings.Repeat("a", 64)+`",
				"closure": "COMPLETE",
				"components": [
					{"kind": "model", "required": true, "present": true},
					{"kind": "optimizer", "required": true, "present": true}
				],
				"reasonCodes": []
			},
			"record": {
				"checkpoint": "/checkpoints/job42",
				"metadata_digest": "sha256:test",
				"latest_mtime": "`+time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)+`",
				"complete": true,
				"ranks": [{
					"rank": 0,
					"shards": [{
						"name": "w",
						"relative_path": "__0_0.distcp",
						"rank": 0,
						"offset": 0,
						"length": 16,
						"size_bytes": 16
					}],
					"bytes": 16
				}],
				"reasons": []
			}
		}`)
	}))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	agent.Listener = listener
	agent.Start()
	defer agent.Close()
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		t.Fatal(err)
	}
	localIP := connection.LocalAddr().(*net.UDPAddr).IP.String()
	connection.Close()
	agentEndpoint := fmt.Sprintf(
		"http://%s:%d",
		localIP,
		listener.Addr().(*net.TCPAddr).Port,
	)

	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 1,
			AgentEndpoint:   agentEndpoint,
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client:     kubeClient,
		Scheme:     scheme,
		AgentToken: "secret",
		HTTPClient: agent.Client(),
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "job42",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if !got.Status.Complete || len(got.Status.Ranks) != 1 {
		t.Fatalf("agent facts not copied: %+v", got.Status)
	}
	if got.Status.Readiness.Status != aevrelv1.ReadinessUnverified {
		t.Fatalf("readiness = %q", got.Status.Readiness.Status)
	}
	if got.Status.Readiness.ResumeClosure != "COMPLETE" {
		t.Fatalf("resume closure = %q", got.Status.Readiness.ResumeClosure)
	}
	if want := "sha256:" + strings.Repeat("a", 64); got.Status.Readiness.ResumeManifestDigest != want {
		t.Fatalf("manifest digest = %q", got.Status.Readiness.ResumeManifestDigest)
	}
	if got.Status.Readiness.StateComplete == nil || !*got.Status.Readiness.StateComplete {
		t.Fatalf("state complete = %v", got.Status.Readiness.StateComplete)
	}
	// No restore drill yet, so the record cannot claim L4 even with a closed
	// closure.
	if got.Status.Readiness.ReadinessLevel != "L3" {
		t.Fatalf("readiness level = %q", got.Status.Readiness.ReadinessLevel)
	}
}

// agentRecordServer serves one checkpoint-record body and returns its URL.
func agentRecordServer(t *testing.T, body string) string {
	t.Helper()
	agent := httptest.NewUnstartedServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.URL.Path != "/v1/checkpoint-record" {
			http.NotFound(writer, request)
			return
		}
		if request.Header.Get("Authorization") != "Bearer secret" {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		fmt.Fprint(writer, body)
	}))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	agent.Listener = listener
	agent.Start()
	t.Cleanup(agent.Close)
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		t.Fatal(err)
	}
	localIP := connection.LocalAddr().(*net.UDPAddr).IP.String()
	connection.Close()
	return fmt.Sprintf("http://%s:%d", localIP, listener.Addr().(*net.TCPAddr).Port)
}

func TestCheckpointReadinessReconcilerGatesOnIncompleteClosure(t *testing.T) {
	mtime := time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
	endpoint := agentRecordServer(t, `{
		"version": "2",
		"resumeState": {
			"manifestDigest": "sha256:`+strings.Repeat("b", 64)+`",
			"closure": "PARTIAL",
			"components": [
				{"kind": "model", "required": true, "present": true},
				{"kind": "dataloader", "required": true, "present": false}
			],
			"reasonCodes": ["RESUME_COMPONENT_MISSING"]
		},
		"record": {
			"checkpoint": "/checkpoints/job42",
			"metadata_digest": "sha256:test",
			"latest_mtime": "`+mtime+`",
			"complete": true,
			"ranks": [{
				"rank": 0,
				"shards": [{
					"name": "w",
					"relative_path": "__0_0.distcp",
					"rank": 0,
					"offset": 0,
					"length": 16,
					"size_bytes": 16
				}],
				"bytes": 16
			}],
			"reasons": []
		}
	}`)

	scheme := runtime.NewScheme()
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	required := true
	record := &aevrelv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: aevrelv1.CheckpointRecordSpec{
			JobRef:               "job42",
			Version:              "step-100",
			Provider:             "dcp",
			FormatVersion:        "1.0.0",
			Checkpoint:           "/checkpoints/job42",
			SourceWorldSize:      1,
			AgentEndpoint:        endpoint,
			RequireStateComplete: &required,
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&aevrelv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client:     kubeClient,
		Scheme:     scheme,
		AgentToken: "secret",
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		ctrl.Request{NamespacedName: types.NamespacedName{
			Namespace: "default",
			Name:      "job42",
		}},
	); err != nil {
		t.Fatal(err)
	}
	var got aevrelv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	readiness := got.Status.Readiness
	if readiness.Status != aevrelv1.ReadinessTargetBlocked {
		t.Fatalf("readiness = %q", readiness.Status)
	}
	if readiness.StateComplete == nil || *readiness.StateComplete {
		t.Fatalf("state complete = %v", readiness.StateComplete)
	}
	if readiness.Loadable == nil || *readiness.Loadable {
		t.Fatalf("loadable must be withdrawn: %v", readiness.Loadable)
	}
	if readiness.ReadinessLevel != "L3" {
		t.Fatalf("readiness level = %q", readiness.ReadinessLevel)
	}
	codes := make([]string, 0, len(readiness.Reasons))
	for _, reason := range readiness.Reasons {
		codes = append(codes, reason.Code)
	}
	if !contains(codes, control.ReasonStateClosureIncomplete) {
		t.Fatalf("reasons = %v", codes)
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func TestControlDrillUsesReferenceAndOutcome(t *testing.T) {
	ready := true
	drill := controlDrill(aevrelv1.CheckpointReadinessStatus{
		Loadable:  &ready,
		LastDrill: "default/restore-drill",
	})
	if drill == nil ||
		drill.Phase != "Succeeded" ||
		drill.ProofRef != "default/restore-drill" {
		t.Fatalf("drill = %+v", drill)
	}

	failed := false
	drill = controlDrill(aevrelv1.CheckpointReadinessStatus{
		Loadable:  &failed,
		LastDrill: "default/restore-drill-failed",
	})
	if drill == nil ||
		drill.Phase != "Failed" ||
		drill.ProofRef != "default/restore-drill-failed" {
		t.Fatalf("drill = %+v", drill)
	}
}

// A drill keeps the identity it proved. Reporting that identity is what lets
// the assessment tell "the drill failed" from "the drill proved a checkpoint we
// no longer have", so the translation must not pre-fail or blank either field.
func TestControlDrillCarriesProvenIdentity(t *testing.T) {
	ready := true
	status := aevrelv1.CheckpointReadinessStatus{
		Loadable:               &ready,
		LastDrill:              "default/restore-drill",
		LastDrillVersion:       "step-100",
		LastDrillDigest:        "sha256:old",
		LastDrillSteps:         3,
		LastDrillRanksReported: []int32{0, 1, 2, 3},
		LastDrillRanksExpected: 4,
	}
	drill := controlDrillForRecord(status, "step-100", "sha256:new")
	if drill == nil {
		t.Fatal("drill = nil")
	}
	if !drill.Succeeded {
		t.Fatalf("translation should not pre-fail the drill: %+v", drill)
	}
	if drill.CheckpointVersion != "step-100" ||
		drill.CheckpointMetadataDigest != "sha256:old" {
		t.Fatalf("identity not carried: %+v", drill)
	}
	if drill.StepsRan != 3 ||
		len(drill.RanksReported) != 4 ||
		drill.RanksExpected != 4 {
		t.Fatalf("coverage not carried: %+v", drill)
	}
}

// The staleness itself is a readiness verdict, so it is asserted end to end:
// a proof bound to a different digest blocks the target and says why.
func TestControlDrillRejectsStaleCheckpointEvidence(t *testing.T) {
	ready := true
	status := aevrelv1.CheckpointReadinessStatus{
		Loadable:               &ready,
		LastDrill:              "default/restore-drill",
		LastDrillVersion:       "step-100",
		LastDrillDigest:        "sha256:old",
		LastDrillSteps:         3,
		LastDrillRanksReported: []int32{0},
		LastDrillRanksExpected: 1,
	}
	drill := controlDrillForRecord(status, "step-100", "sha256:new")
	readiness := control.AssessReadiness(control.ReadinessInput{
		Record: control.CheckpointRecord{
			Version:        "step-100",
			MetadataDigest: "sha256:new",
			LatestMtime:    time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Complete:       true,
			Ranks:          []control.RankInventory{{Rank: 0, Bytes: 16}},
		},
		Now:   time.Now().UTC(),
		Drill: drill,
	})
	if readiness.Status != control.ReadinessTargetBlocked {
		t.Fatalf("status = %q, readiness = %+v", readiness.Status, readiness)
	}
	if readiness.Loadable == nil || *readiness.Loadable {
		t.Fatalf("loadable should have been withdrawn: %+v", readiness)
	}
	codes := make([]string, 0, len(readiness.Reasons))
	for _, reason := range readiness.Reasons {
		codes = append(codes, reason.Code)
	}
	if !contains(codes, control.ReasonDrillProofStale) {
		t.Fatalf("reasons = %v", codes)
	}

	// The same drill against the checkpoint it actually proved still counts.
	readiness = control.AssessReadiness(control.ReadinessInput{
		Record: control.CheckpointRecord{
			Version:        "step-100",
			MetadataDigest: "sha256:old",
			LatestMtime:    time.Now().Add(-time.Minute).UTC().Format(time.RFC3339),
			Complete:       true,
			Ranks:          []control.RankInventory{{Rank: 0, Bytes: 16}},
		},
		Now:   time.Now().UTC(),
		Drill: drill,
	})
	if readiness.Status != control.ReadinessReady ||
		readiness.Loadable == nil ||
		!*readiness.Loadable {
		t.Fatalf("matching drill = %+v", readiness)
	}
}
