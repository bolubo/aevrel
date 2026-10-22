package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/clusterclient"
)

type fakeClusterInfo struct {
	info clusterclient.GatewayInfo
	err  error
}

func (f fakeClusterInfo) Info(
	context.Context,
	string,
	string,
) (clusterclient.GatewayInfo, error) {
	return f.info, f.err
}

func TestClusterHealthReconcilerMarksReady(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	cluster := &shardferryv1.ShardferryCluster{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "source",
			Namespace: "shardferry-control",
		},
		Spec: shardferryv1.ShardferryClusterSpec{
			Endpoint: "https://gateway.example",
			SecretRef: shardferryv1.ClusterSecretRef{
				Name: "source-auth",
			},
		},
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "source-auth",
			Namespace: "shardferry-control",
		},
		Data: map[string][]byte{"token": []byte("gateway-token")},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.ShardferryCluster{}).
		WithObjects(cluster, secret).
		Build()
	reconciler := &ClusterHealthReconciler{
		Client: kubeClient,
		Clusters: fakeClusterInfo{
			info: clusterclient.GatewayInfo{
				Version:        "1",
				GatewayVersion: "0.2.0",
			},
		},
		RequeueAfter: time.Second,
	}
	_, err := reconciler.Reconcile(context.Background(), reconcile.Request{
		NamespacedName: types.NamespacedName{
			Name:      "source",
			Namespace: "shardferry-control",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.ShardferryCluster
	if err := kubeClient.Get(context.Background(), types.NamespacedName{
		Name:      "source",
		Namespace: "shardferry-control",
	}, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Status.Ready {
		t.Fatalf("status.ready = false: %#v", got.Status)
	}
	if got.Status.Version != "0.2.0" {
		t.Fatalf("status.version = %q", got.Status.Version)
	}
	if got.Status.LastSeen == "" {
		t.Fatal("status.lastSeen is empty")
	}
}

func TestClusterHealthReconcilerMarksFailure(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	cluster := &shardferryv1.ShardferryCluster{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "target",
			Namespace: "shardferry-control",
		},
		Spec: shardferryv1.ShardferryClusterSpec{
			Endpoint: "https://gateway.example",
			SecretRef: shardferryv1.ClusterSecretRef{
				Name: "target-auth",
			},
		},
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "target-auth",
			Namespace: "shardferry-control",
		},
		Data: map[string][]byte{"token": []byte("gateway-token")},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&shardferryv1.ShardferryCluster{}).
		WithObjects(cluster, secret).
		Build()
	reconciler := &ClusterHealthReconciler{
		Client:       kubeClient,
		Clusters:     fakeClusterInfo{err: errors.New("gateway is unreachable")},
		RequeueAfter: time.Second,
	}
	_, err := reconciler.Reconcile(context.Background(), reconcile.Request{
		NamespacedName: types.NamespacedName{
			Name:      "target",
			Namespace: "shardferry-control",
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got shardferryv1.ShardferryCluster
	if err := kubeClient.Get(context.Background(), types.NamespacedName{
		Name:      "target",
		Namespace: "shardferry-control",
	}, &got); err != nil {
		t.Fatal(err)
	}
	if got.Status.Ready {
		t.Fatalf("status.ready = true: %#v", got.Status)
	}
	if got.Status.Message == "" {
		t.Fatal("status.message is empty")
	}
}
