package controller

import (
	"context"
	"fmt"
	"net"
	"sort"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/clusterclient"
)

type RankAPI interface {
	Ranks(context.Context, string) ([]int32, error)
}

type ClusterAPI interface {
	Resolve(context.Context, string, string, clusterclient.ResolveRequest) (clusterclient.ResolveResponse, error)
}

type EndpointTokenRegistrar interface {
	RegisterEndpointToken(string, string)
}

// AgentResolver expands label-selector based agent discovery into rank maps.
type AgentResolver struct {
	Client   client.Client
	Reader   client.Reader
	Agents   RankAPI
	Clusters ClusterAPI
	Tokens   EndpointTokenRegistrar
}

func (r *AgentResolver) EnsureTokens(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
) error {
	for _, ref := range []string{
		migration.Spec.Source.ClusterRef,
		migration.Spec.Target.ClusterRef,
	} {
		if ref == "" {
			continue
		}
		cluster, token, err := r.clusterCredential(
			ctx,
			migration.Namespace,
			ref,
		)
		if err != nil {
			return err
		}
		r.registerToken(cluster.Spec.Endpoint, token)
	}
	return nil
}

func (r *AgentResolver) Resolve(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
) (*shardferryv1.CheckpointMigration, error) {
	effective := migration.DeepCopy()
	if len(effective.Spec.Source.RankMap) == 0 {
		var ranks []shardferryv1.RankEndpoint
		var err error
		if effective.Spec.Source.ClusterRef != "" {
			ranks, err = r.remoteRanks(
				ctx,
				effective,
				"source",
				effective.Spec.Source.ClusterRef,
				effective.Spec.Source.AgentSelector,
				effective.Spec.Source.WorldSize,
			)
		} else {
			ranks, err = r.sourceRanks(ctx, effective, effective.Spec.Source.AgentSelector)
		}
		if err != nil {
			return nil, err
		}
		effective.Spec.Source.RankMap = ranks
		effective.Spec.Source.AgentSelector = nil
		effective.Spec.Source.ClusterRef = ""
	}
	if len(effective.Spec.Target.RankMap) == 0 {
		var ranks []shardferryv1.RankEndpoint
		var err error
		if effective.Spec.Target.ClusterRef != "" {
			ranks, err = r.remoteRanks(
				ctx,
				effective,
				"target",
				effective.Spec.Target.ClusterRef,
				effective.Spec.Target.AgentSelector,
				effective.Spec.Target.WorldSize,
			)
		} else {
			ranks, err = r.targetRanks(ctx, effective, effective.Spec.Target.AgentSelector)
		}
		if err != nil {
			return nil, err
		}
		effective.Spec.Target.RankMap = ranks
		effective.Spec.Target.AgentSelector = nil
		effective.Spec.Target.ClusterRef = ""
	}
	return effective, nil
}

func (r *AgentResolver) ResolveTarget(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
) ([]shardferryv1.RankEndpoint, error) {
	if len(migration.Spec.Target.RankMap) > 0 {
		return append([]shardferryv1.RankEndpoint(nil), migration.Spec.Target.RankMap...), nil
	}
	if migration.Spec.Target.ClusterRef != "" {
		return r.remoteRanks(
			ctx,
			migration,
			"target",
			migration.Spec.Target.ClusterRef,
			migration.Spec.Target.AgentSelector,
			migration.Spec.Target.WorldSize,
		)
	}
	return r.targetRanks(ctx, migration, migration.Spec.Target.AgentSelector)
}

func (r *AgentResolver) remoteRanks(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	role string,
	clusterRef string,
	selector *shardferryv1.AgentSelector,
	worldSize int32,
) ([]shardferryv1.RankEndpoint, error) {
	if r.Clusters == nil {
		return nil, fmt.Errorf("cluster gateway client is not configured")
	}
	if selector == nil || len(selector.MatchLabels) == 0 {
		return nil, fmt.Errorf("%s.agentSelector.matchLabels is required", role)
	}
	cluster, token, err := r.clusterCredential(ctx, migration.Namespace, clusterRef)
	if err != nil {
		return nil, fmt.Errorf("%s cluster %s: %w", role, clusterRef, err)
	}
	if cluster.Spec.Role != "" &&
		cluster.Spec.Role != "both" &&
		cluster.Spec.Role != role {
		return nil, fmt.Errorf(
			"%s cluster %s has role %q",
			role,
			clusterRef,
			cluster.Spec.Role,
		)
	}
	if cluster.Spec.Endpoint == "" {
		return nil, fmt.Errorf("cluster %s has no gateway endpoint", clusterRef)
	}
	r.registerToken(cluster.Spec.Endpoint, token)
	port := selector.Port
	if port == 0 {
		port = 9443
	}
	scheme := selector.Scheme
	if scheme == "" {
		scheme = "http"
	}
	response, err := r.Clusters.Resolve(ctx, cluster.Spec.Endpoint, token, clusterclient.ResolveRequest{
		Version:     "1",
		Role:        role,
		Namespace:   selector.Namespace,
		MatchLabels: selector.MatchLabels,
		Port:        port,
		Scheme:      scheme,
		WorldSize:   worldSize,
	})
	if err != nil {
		return nil, fmt.Errorf("%s cluster %s resolve: %w", role, clusterRef, err)
	}
	if len(response.Ranks) != int(worldSize) {
		return nil, fmt.Errorf(
			"%s cluster %s returned %d ranks, want %d",
			role,
			clusterRef,
			len(response.Ranks),
			worldSize,
		)
	}
	result := make([]shardferryv1.RankEndpoint, 0, len(response.Ranks))
	for _, entry := range response.Ranks {
		if entry.Rank < 0 || entry.Rank >= worldSize {
			return nil, fmt.Errorf(
				"%s cluster %s returned out-of-range rank %d",
				role,
				clusterRef,
				entry.Rank,
			)
		}
		if entry.Endpoint == "" {
			return nil, fmt.Errorf(
				"%s cluster %s returned rank %d without endpoint",
				role,
				clusterRef,
				entry.Rank,
			)
		}
		result = append(result, shardferryv1.RankEndpoint{
			Rank:     entry.Rank,
			Endpoint: entry.Endpoint,
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Rank < result[j].Rank
	})
	return result, nil
}

func (r *AgentResolver) clusterCredential(
	ctx context.Context,
	namespace string,
	clusterRef string,
) (shardferryv1.ShardferryCluster, string, error) {
	reader := r.Reader
	if reader == nil {
		reader = r.Client
	}
	var cluster shardferryv1.ShardferryCluster
	if err := reader.Get(
		ctx,
		client.ObjectKey{Namespace: namespace, Name: clusterRef},
		&cluster,
	); err != nil {
		return shardferryv1.ShardferryCluster{}, "", err
	}
	token, err := r.clusterToken(ctx, reader, namespace, cluster.Spec.SecretRef)
	if err != nil {
		return shardferryv1.ShardferryCluster{}, "", err
	}
	return cluster, token, nil
}

func (r *AgentResolver) registerToken(endpoint, token string) {
	if r.Tokens != nil {
		r.Tokens.RegisterEndpointToken(endpoint, token)
	}
}

func (r *AgentResolver) clusterToken(
	ctx context.Context,
	reader client.Reader,
	namespace string,
	ref shardferryv1.ClusterSecretRef,
) (string, error) {
	if ref.Name == "" {
		return "", fmt.Errorf("secretRef.name is required")
	}
	key := ref.Key
	if key == "" {
		key = "token"
	}
	var secret corev1.Secret
	if err := reader.Get(
		ctx,
		client.ObjectKey{Namespace: namespace, Name: ref.Name},
		&secret,
	); err != nil {
		return "", err
	}
	value := secret.Data[key]
	if len(value) == 0 {
		return "", fmt.Errorf("secret %s/%s key %s is empty", namespace, ref.Name, key)
	}
	return string(value), nil
}

func (r *AgentResolver) sourceRanks(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	selector *shardferryv1.AgentSelector,
) ([]shardferryv1.RankEndpoint, error) {
	pods, err := r.agentPods(ctx, migration.Namespace, selector)
	if err != nil {
		return nil, fmt.Errorf("discover source agents: %w", err)
	}
	assigned := make(map[int32]string, migration.Spec.Source.WorldSize)
	for _, pod := range pods {
		endpoint, err := agentEndpoint(pod, selector)
		if err != nil {
			return nil, fmt.Errorf("source pod %s: %w", pod.Name, err)
		}
		if r.Agents == nil {
			return nil, fmt.Errorf("rank discovery API is not configured")
		}
		ranks, err := r.Agents.Ranks(ctx, endpoint)
		if err != nil {
			return nil, fmt.Errorf("source pod %s ranks: %w", pod.Name, err)
		}
		for _, rank := range ranks {
			if rank < 0 || rank >= migration.Spec.Source.WorldSize {
				return nil, fmt.Errorf(
					"source pod %s reports out-of-range rank %d",
					pod.Name,
					rank,
				)
			}
			if previous, exists := assigned[rank]; exists {
				return nil, fmt.Errorf(
					"source rank %d is served by both %s and %s",
					rank,
					previous,
					endpoint,
				)
			}
			assigned[rank] = endpoint
		}
	}
	return orderedRanks("source", migration.Spec.Source.WorldSize, assigned)
}

func (r *AgentResolver) targetRanks(
	ctx context.Context,
	migration *shardferryv1.CheckpointMigration,
	selector *shardferryv1.AgentSelector,
) ([]shardferryv1.RankEndpoint, error) {
	pods, err := r.agentPods(ctx, migration.Namespace, selector)
	if err != nil {
		return nil, fmt.Errorf("discover target agents: %w", err)
	}
	endpoints := make([]string, 0, len(pods))
	for _, pod := range pods {
		endpoint, err := agentEndpoint(pod, selector)
		if err != nil {
			return nil, fmt.Errorf("target pod %s: %w", pod.Name, err)
		}
		endpoints = append(endpoints, endpoint)
	}
	result := make([]shardferryv1.RankEndpoint, 0, migration.Spec.Target.WorldSize)
	for rank := int32(0); rank < migration.Spec.Target.WorldSize; rank++ {
		result = append(result, shardferryv1.RankEndpoint{
			Rank:     rank,
			Endpoint: endpoints[int(rank)%len(endpoints)],
		})
	}
	return result, nil
}

func (r *AgentResolver) agentPods(
	ctx context.Context,
	migrationNamespace string,
	selector *shardferryv1.AgentSelector,
) ([]corev1.Pod, error) {
	if selector == nil || len(selector.MatchLabels) == 0 {
		return nil, fmt.Errorf("agentSelector.matchLabels is required")
	}
	if r.Client == nil {
		return nil, fmt.Errorf("Kubernetes client is not configured")
	}
	namespace := selector.Namespace
	if namespace == "" {
		namespace = migrationNamespace
	}
	var pods corev1.PodList
	if err := r.Client.List(
		ctx,
		&pods,
		client.InNamespace(namespace),
		client.MatchingLabels(selector.MatchLabels),
	); err != nil {
		return nil, err
	}
	available := pods.Items[:0]
	for _, pod := range pods.Items {
		if pod.DeletionTimestamp != nil || pod.Status.PodIP == "" {
			continue
		}
		if pod.Status.Phase != "" && pod.Status.Phase != corev1.PodRunning {
			continue
		}
		if !podReady(pod) {
			continue
		}
		available = append(available, pod)
	}
	if len(available) == 0 {
		return nil, fmt.Errorf(
			"no ready agent pods match %v in namespace %s",
			selector.MatchLabels,
			namespace,
		)
	}
	sort.Slice(available, func(i, j int) bool {
		return available[i].Name < available[j].Name
	})
	return available, nil
}

func podReady(pod corev1.Pod) bool {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return true
}

func agentEndpoint(
	pod corev1.Pod,
	selector *shardferryv1.AgentSelector,
) (string, error) {
	if selector == nil {
		return "", fmt.Errorf("agentSelector is required")
	}
	if pod.Status.PodIP == "" {
		return "", fmt.Errorf("pod has no IP")
	}
	scheme := selector.Scheme
	if scheme == "" {
		scheme = "http"
	}
	port := selector.Port
	if port == 0 {
		port = 9443
	}
	return scheme + "://" + net.JoinHostPort(
		pod.Status.PodIP,
		strconv.Itoa(int(port)),
	), nil
}

func orderedRanks(
	role string,
	worldSize int32,
	assigned map[int32]string,
) ([]shardferryv1.RankEndpoint, error) {
	result := make([]shardferryv1.RankEndpoint, 0, worldSize)
	missing := make([]int32, 0)
	for rank := int32(0); rank < worldSize; rank++ {
		endpoint, exists := assigned[rank]
		if !exists {
			missing = append(missing, rank)
			continue
		}
		result = append(result, shardferryv1.RankEndpoint{
			Rank:     rank,
			Endpoint: endpoint,
		})
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf(
			"%s rank discovery missing ranks: %v",
			role,
			missing,
		)
	}
	return result, nil
}
