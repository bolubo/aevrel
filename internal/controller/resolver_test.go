package controller

import (
	"context"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/clusterclient"
)

type fakeRankAPI map[string][]int32

func (f fakeRankAPI) Ranks(_ context.Context, endpoint string) ([]int32, error) {
	return f[endpoint], nil
}

type fakeClusterAPI map[string]clusterclient.ResolveResponse

func (f fakeClusterAPI) Resolve(
	_ context.Context,
	endpoint string,
	_ string,
	request clusterclient.ResolveRequest,
) (clusterclient.ResolveResponse, error) {
	response, ok := f[endpoint]
	if !ok {
		return clusterclient.ResolveResponse{}, errTest("unknown endpoint")
	}
	if response.Version == "" {
		response.Version = "1"
	}
	if request.Role == "source" && len(response.Ranks) == 0 {
		return clusterclient.ResolveResponse{}, errTest("no source ranks")
	}
	return response, nil
}

type errTest string

func (e errTest) Error() string { return string(e) }

func TestAgentResolverBuildsSourceAndTargetRankMaps(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	selector := &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel", "role": "source"},
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
	migration.Spec.Target.Mesh = aevrelv1.MeshSpec{
		Shape:      []int32{3},
		Placements: []string{"Shard(0)"},
	}
	migration.Spec.Target.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel", "role": "target"},
		Port:        9444,
		Scheme:      "http",
	}

	resolved, err := resolver.Resolve(context.Background(), migration)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := resolved.Spec.Source.RankMap, []aevrelv1.RankEndpoint{
		{Rank: 0, Endpoint: "https://10.0.0.1:9443"},
		{Rank: 1, Endpoint: "https://10.0.0.2:9443"},
	}; !reflect.DeepEqual(got, want) {
		t.Fatalf("source rank map = %#v, want %#v", got, want)
	}
	if got, want := resolved.Spec.Target.RankMap, []aevrelv1.RankEndpoint{
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
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel"},
	}
	if _, err := resolver.Resolve(context.Background(), migration); err == nil {
		t.Fatal("missing source rank must fail")
	}
}

func TestAgentResolverUsesClusterGateways(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := aevrelv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	migration := validMigration("remote")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.ClusterRef = "source-cluster"
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"role": "source"},
	}
	migration.Spec.Target.RankMap = nil
	migration.Spec.Target.ClusterRef = "target-cluster"
	migration.Spec.Target.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"role": "target"},
	}
	migration.Spec.Target.WorldSize = 2
	migration.Spec.Target.Mesh = aevrelv1.MeshSpec{
		Shape:      []int32{2},
		Placements: []string{"Shard(0)"},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(
			&aevrelv1.AevrelCluster{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Name:      "source-cluster",
				},
				Spec: aevrelv1.AevrelClusterSpec{
					Role:     "source",
					Endpoint: "https://source.example",
					SecretRef: aevrelv1.ClusterSecretRef{
						Name: "source-auth",
					},
				},
			},
			&aevrelv1.AevrelCluster{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Name:      "target-cluster",
				},
				Spec: aevrelv1.AevrelClusterSpec{
					Role:     "target",
					Endpoint: "https://target.example",
					SecretRef: aevrelv1.ClusterSecretRef{
						Name: "target-auth",
					},
				},
			},
			&corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Name:      "source-auth",
				},
				Data: map[string][]byte{"token": []byte("source-token")},
			},
			&corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Name:      "target-auth",
				},
				Data: map[string][]byte{"token": []byte("target-token")},
			},
		).
		Build()
	resolver := &AgentResolver{
		Client: kubeClient,
		Clusters: fakeClusterAPI{
			"https://source.example": {
				Ranks: []clusterclient.RankEndpoint{
					{Rank: 0, Endpoint: "https://gateway/source/0"},
					{Rank: 1, Endpoint: "https://gateway/source/1"},
				},
			},
			"https://target.example": {
				Ranks: []clusterclient.RankEndpoint{
					{Rank: 0, Endpoint: "https://gateway/target/0"},
					{Rank: 1, Endpoint: "https://gateway/target/1"},
				},
			},
		},
	}
	resolved, err := resolver.Resolve(context.Background(), migration)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateMigration(resolved); err != nil {
		t.Fatalf("resolved migration is invalid: %v", err)
	}
	if len(resolved.Spec.Source.RankMap) != 2 ||
		resolved.Spec.Source.RankMap[1].Endpoint != "https://gateway/source/1" {
		t.Fatalf("source rank map = %#v", resolved.Spec.Source.RankMap)
	}
	if len(resolved.Spec.Target.RankMap) != 2 ||
		resolved.Spec.Target.RankMap[1].Endpoint != "https://gateway/target/1" {
		t.Fatalf("target rank map = %#v", resolved.Spec.Target.RankMap)
	}
}

func TestValidateMigrationAllowsClusterRefSelector(t *testing.T) {
	migration := validMigration("remote-valid")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.ClusterRef = "source"
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"role": "source"},
	}
	migration.Spec.Target.RankMap = nil
	migration.Spec.Target.ClusterRef = "target"
	migration.Spec.Target.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"role": "target"},
	}
	if err := validateMigration(migration); err != nil {
		t.Fatal(err)
	}
}

func TestValidateMigrationRejectsClusterRefWithRankMap(t *testing.T) {
	migration := validMigration("remote-bad")
	migration.Spec.Source.ClusterRef = "source"
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"role": "source"},
	}
	if err := validateMigration(migration); err == nil {
		t.Fatal("clusterRef with rankMap must fail")
	}
}

func TestValidateAgentSelectorRejectsRankMapAndSelectorTogether(t *testing.T) {
	migration := validMigration("both")
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel"},
	}
	if err := validateMigration(migration); err == nil {
		t.Fatal("rankMap together with agentSelector must fail")
	}
}

func TestValidateMigrationAcceptsSelectorInsteadOfRankMap(t *testing.T) {
	migration := validMigration("selector-only")
	migration.Spec.Source.RankMap = nil
	migration.Spec.Source.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel"},
	}
	migration.Spec.Target.RankMap = nil
	migration.Spec.Target.AgentSelector = &aevrelv1.AgentSelector{
		MatchLabels: map[string]string{"app": "aevrel"},
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
				"app":  "aevrel",
				"role": role,
			},
		},
		Status: corev1.PodStatus{PodIP: ip},
	}
}
