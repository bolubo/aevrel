package controller

import (
	"context"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

type fakeRankAPI map[string][]int32

func (f fakeRankAPI) Ranks(_ context.Context, endpoint string) ([]int32, error) {
	return f[endpoint], nil
}

func TestAgentResolverBuildsSourceAndTargetRankMaps(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	selector := &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry", "role": "source"},
		Port:        9443,
		Scheme:      "https",
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(
			agentPod("source-b", "10.0.0.2", "source"),
			agentPod("source-a", "10.0.0.1", "source"),
			agentPod("target-a", "10.0.0.3", "target"),
			agentPod("target-b", "10.0.0.4", "target"),
		).
		Build()
	resolver := &AgentResolver{
		Client: kubeClient,
		Agents: fakeRankAPI{
			"https://10.0.0.1:9443": {0},
			"https://10.0.0.2:9443": {1},
		},
	}
	migration := validMigration("selector")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.AgentSelector = selector
	migration.Spec.Target.RankMap = nil
	migration.Spec.Target.WorldSize = 3
	migration.Spec.Target.Mesh = shardferryv1.MeshSpec{
		Shape:      []int32{3},
		Placements: []string{"Shard(0)"},
	}
	migration.Spec.Target.AgentSelector = &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry", "role": "target"},
		Port:        9444,
		Scheme:      "http",
	}

	resolved, err := resolver.Resolve(context.Background(), migration)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := resolved.Spec.Source.RankMap, []shardferryv1.RankEndpoint{
		{Rank: 0, Endpoint: "https://10.0.0.1:9443"},
		{Rank: 1, Endpoint: "https://10.0.0.2:9443"},
	}; !reflect.DeepEqual(got, want) {
		t.Fatalf("source rank map = %#v, want %#v", got, want)
	}
	if got, want := resolved.Spec.Target.RankMap, []shardferryv1.RankEndpoint{
		{Rank: 0, Endpoint: "http://10.0.0.3:9444"},
		{Rank: 1, Endpoint: "http://10.0.0.4:9444"},
		{Rank: 2, Endpoint: "http://10.0.0.3:9444"},
	}; !reflect.DeepEqual(got, want) {
		t.Fatalf("target rank map = %#v, want %#v", got, want)
	}
}

func TestAgentResolverFailsOnMissingSourceRank(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agentPod("source-a", "10.0.0.1", "source")).
		Build()
	resolver := &AgentResolver{
		Client: kubeClient,
		Agents: fakeRankAPI{"http://10.0.0.1:9443": {0}},
	}
	migration := validMigration("missing")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.AgentSelector = &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry"},
	}
	if _, err := resolver.Resolve(context.Background(), migration); err == nil {
		t.Fatal("missing source rank must fail")
	}
}

func TestValidateAgentSelectorRejectsRankMapAndSelectorTogether(t *testing.T) {
	migration := validMigration("both")
	migration.Spec.Source.AgentSelector = &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry"},
	}
	if err := validateMigration(migration); err == nil {
		t.Fatal("rankMap together with agentSelector must fail")
	}
}

func TestValidateMigrationAcceptsSelectorInsteadOfRankMap(t *testing.T) {
	migration := validMigration("selector-only")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.AgentSelector = &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry"},
	}
	migration.Spec.Target.RankMap = nil
	migration.Spec.Target.AgentSelector = &shardferryv1.AgentSelector{
		MatchLabels: map[string]string{"app": "shardferry"},
	}
	if err := validateMigration(migration); err != nil {
		t.Fatal(err)
	}
}

func agentPod(name, ip, role string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: "default",
			Name:      name,
			Labels: map[string]string{
				"app":  "shardferry",
				"role": role,
			},
		},
		Status: corev1.PodStatus{PodIP: ip},
	}
}
