package controller

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func TestApplyAgentCheckpointFactsMerges128Ranks(t *testing.T) {
	record := &shardferryv1.CheckpointRecord{
		Spec: shardferryv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			SourceWorldSize: 128,
		},
	}
	first := agentCheckpointFact{
		Version:        "step-100",
		Provider:       "dcp",
		FormatVersion:  "1.0.0",
		MetadataDigest: "sha256:same",
		LatestMtime:    "2026-10-22T00:00:00+00:00",
		Ranks:          agentRanks(0, 64),
		Reasons: []agentReasonFact{{
			Code:   "RANK_MISSING",
			Detail: "rank 64 has no local shard files",
		}},
	}
	second := agentCheckpointFact{
		Version:        "step-100",
		Provider:       "dcp",
		FormatVersion:  "1.0.0",
		MetadataDigest: "sha256:same",
		LatestMtime:    "2026-10-22T00:00:01+00:00",
		Ranks:          agentRanks(64, 128),
	}
	err := applyAgentCheckpointFacts(record, []checkpointAgentObservation{
		{Endpoint: "agent-a", Fact: first},
		{Endpoint: "agent-b", Fact: second},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !record.Status.Complete {
		t.Fatalf("record is incomplete: %+v", record.Status.Readiness.Reasons)
	}
	if len(record.Status.Ranks) != 128 {
		t.Fatalf("ranks = %d, want 128", len(record.Status.Ranks))
	}
	if record.Status.MetadataDigest != "sha256:same" ||
		record.Status.LatestMtime != "2026-10-22T00:00:01+00:00" {
		t.Fatalf("record status = %+v", record.Status)
	}
	for index, rank := range record.Status.Ranks {
		if rank.Rank != int32(index) ||
			len(rank.Shards) != 1 ||
			rank.Bytes != 16 {
			t.Fatalf("rank %d = %+v", index, rank)
		}
	}
}

func TestApplyAgentCheckpointFactsFailsClosedOnConflicts(t *testing.T) {
	record := &shardferryv1.CheckpointRecord{
		Spec: shardferryv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			SourceWorldSize: 2,
		},
	}
	first := agentCheckpointFact{
		Version:        "step-100",
		Provider:       "dcp",
		FormatVersion:  "1.0.0",
		MetadataDigest: "sha256:a",
		Ranks:          agentRanks(0, 2),
	}
	second := first
	second.MetadataDigest = "sha256:b"
	second.Ranks = agentRanks(0, 2)
	second.Ranks[0].Shards[0].Length = 32
	err := applyAgentCheckpointFacts(record, []checkpointAgentObservation{
		{Endpoint: "agent-a", Fact: first},
		{Endpoint: "agent-b", Fact: second},
	})
	if err != nil {
		t.Fatal(err)
	}
	if record.Status.Complete {
		t.Fatal("conflicting facts must be incomplete")
	}
	codes := make(map[string]bool)
	for _, reason := range record.Status.Readiness.Reasons {
		codes[reason.Code] = true
	}
	if !codes["METADATA_MISMATCH"] || !codes["RANK_DUPLICATE"] {
		t.Fatalf("reasons = %+v", record.Status.Readiness.Reasons)
	}
}

func TestCheckpointReadinessReconcilerUsesAgentSelector(t *testing.T) {
	agent := httptest.NewUnstartedServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.URL.Path != "/v1/checkpoint-record" {
			http.NotFound(writer, request)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		fmt.Fprint(writer, checkpointScanResponse(2))
	}))
	listener, err := net.Listen("tcp", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	agent.Listener = listener
	agent.Start()
	defer agent.Close()
	localIP := testLocalIP(t)
	port := int32(listener.Addr().(*net.TCPAddr).Port)

	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: shardferryv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 2,
			AgentSelector: &shardferryv1.AgentSelector{
				MatchLabels: map[string]string{"app": "shardferry-agent"},
				Port:        port,
				Scheme:      "http",
			},
		},
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "agent-a",
			Namespace: "default",
			Labels:    map[string]string{"app": "shardferry-agent"},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
			PodIP: localIP,
			Conditions: []corev1.PodCondition{{
				Type:   corev1.PodReady,
				Status: corev1.ConditionTrue,
			}},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointRecord{}).
		WithObjects(record, pod).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client:     kubeClient,
		Scheme:     scheme,
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
	var got shardferryv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if !got.Status.Complete || len(got.Status.Ranks) != 2 {
		t.Fatalf("selector facts not merged: %+v", got.Status)
	}
	if got.Status.Readiness.Status != shardferryv1.ReadinessUnverified {
		t.Fatalf("readiness = %q", got.Status.Readiness.Status)
	}
}

func TestCheckpointReadinessReconcilerPreservesFactsWhenAgentUnavailable(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	record := &shardferryv1.CheckpointRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "job42", Namespace: "default"},
		Spec: shardferryv1.CheckpointRecordSpec{
			JobRef:          "job42",
			Version:         "step-100",
			Provider:        "dcp",
			FormatVersion:   "1.0.0",
			Checkpoint:      "/checkpoints/job42",
			SourceWorldSize: 1,
			AgentEndpoint:   "http://127.0.0.1:1",
		},
		Status: shardferryv1.CheckpointRecordStatus{
			MetadataDigest: "sha256:last-known",
			LatestMtime:    "2026-10-22T00:00:00+00:00",
			Complete:       true,
			Ranks: []shardferryv1.CheckpointRankStatus{{
				Rank:  0,
				Bytes: 16,
			}},
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.CheckpointRecord{}).
		WithObjects(record).
		Build()
	reconciler := &CheckpointReadinessReconciler{
		Client:     kubeClient,
		Scheme:     scheme,
		HTTPClient: &http.Client{Timeout: 200 * time.Millisecond},
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
	var got shardferryv1.CheckpointRecord
	if err := kubeClient.Get(
		context.Background(),
		types.NamespacedName{Namespace: "default", Name: "job42"},
		&got,
	); err != nil {
		t.Fatal(err)
	}
	if got.Status.MetadataDigest != "sha256:last-known" ||
		len(got.Status.Ranks) != 1 ||
		!got.Status.Complete {
		t.Fatalf("last known facts were lost: %+v", got.Status)
	}
	if got.Status.Readiness.Status != shardferryv1.ReadinessUnknown {
		t.Fatalf("readiness = %q", got.Status.Readiness.Status)
	}
	if len(got.Status.Readiness.Reasons) != 1 ||
		got.Status.Readiness.Reasons[0].Code != "AGENT_UNREACHABLE" {
		t.Fatalf("reasons = %+v", got.Status.Readiness.Reasons)
	}
}

func agentRanks(start, end int32) []agentRankFact {
	result := make([]agentRankFact, 0, end-start)
	for rank := start; rank < end; rank++ {
		result = append(result, agentRankFact{
			Rank: rank,
			Shards: []agentShardFact{{
				Name:         fmt.Sprintf("weight.%d", rank),
				RelativePath: fmt.Sprintf("__%d_0.distcp", rank),
				Rank:         rank,
				Offset:       0,
				Length:       16,
				SizeBytes:    16,
			}},
			Bytes: 16,
		})
	}
	return result
}

func checkpointScanResponse(worldSize int) string {
	ranks := make([]string, 0, worldSize)
	for rank := 0; rank < worldSize; rank++ {
		ranks = append(ranks, fmt.Sprintf(`{
			"rank": %d,
			"shards": [{
				"name": "weight.%d",
				"relative_path": "__%d_0.distcp",
				"rank": %d,
				"offset": 0,
				"length": 16,
				"size_bytes": 16
			}],
			"bytes": 16
		}`, rank, rank, rank, rank))
	}
	return fmt.Sprintf(`{
		"version": "1",
		"record": {
			"checkpoint": "/checkpoints/job42",
			"namespace": "default",
			"job_uid": "job42",
			"version": "step-100",
			"provider": "dcp",
			"format_version": "1.0.0",
			"metadata_digest": "sha256:test",
			"latest_mtime": "%s",
			"complete": true,
			"ranks": [%s],
			"reasons": []
		}
	}`, time.Now().Add(-time.Minute).UTC().Format(time.RFC3339), joinJSON(ranks))
}

func joinJSON(items []string) string {
	result := ""
	for index, item := range items {
		if index > 0 {
			result += ","
		}
		result += item
	}
	return result
}

func testLocalIP(t *testing.T) string {
	t.Helper()
	connection, err := net.Dial("udp", "1.1.1.1:80")
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	return connection.LocalAddr().(*net.UDPAddr).IP.String()
}
