package controller

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/clusterclient"
)

// ClusterInfoAPI is the gateway health/info surface used by the cluster
// status reconciler.
type ClusterInfoAPI interface {
	Info(ctx context.Context, endpoint string, token string) (clusterclient.GatewayInfo, error)
}

// ClusterHealthReconciler keeps AevrelCluster status useful for users and
// preflight. It only probes the gateway; it does not discover ranks.
type ClusterHealthReconciler struct {
	client.Client
	Reader       client.Reader
	Clusters     ClusterInfoAPI
	RequeueAfter time.Duration
}

func (r *ClusterHealthReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var cluster aevrelv1.AevrelCluster
	if err := r.Get(ctx, request.NamespacedName, &cluster); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}
	requeue := r.RequeueAfter
	if requeue <= 0 {
		requeue = 30 * time.Second
	}
	if cluster.Spec.Endpoint == "" {
		return ctrl.Result{RequeueAfter: requeue}, r.setStatus(
			ctx,
			&cluster,
			false,
			"",
			"gateway endpoint is required",
		)
	}
	var secret corev1.Secret
	key := types.NamespacedName{
		Namespace: cluster.Namespace,
		Name:      cluster.Spec.SecretRef.Name,
	}
	reader := r.Reader
	if reader == nil {
		reader = r.Client
	}
	if err := reader.Get(ctx, key, &secret); err != nil {
		return ctrl.Result{RequeueAfter: requeue}, r.setStatus(
			ctx,
			&cluster,
			false,
			"",
			fmt.Sprintf("read gateway secret: %v", err),
		)
	}
	secretKey := cluster.Spec.SecretRef.Key
	if secretKey == "" {
		secretKey = "token"
	}
	token := string(secret.Data[secretKey])
	if token == "" {
		return ctrl.Result{RequeueAfter: requeue}, r.setStatus(
			ctx,
			&cluster,
			false,
			"",
			fmt.Sprintf("secret %s/%s has no %s", key.Namespace, key.Name, secretKey),
		)
	}
	if r.Clusters == nil {
		return ctrl.Result{}, fmt.Errorf("cluster info client is not configured")
	}
	info, err := r.Clusters.Info(ctx, cluster.Spec.Endpoint, token)
	if err != nil {
		return ctrl.Result{RequeueAfter: requeue}, r.setStatus(
			ctx,
			&cluster,
			false,
			"",
			err.Error(),
		)
	}
	return ctrl.Result{RequeueAfter: requeue}, r.setStatus(
		ctx,
		&cluster,
		true,
		info.GatewayVersion,
		"",
	)
}

func (r *ClusterHealthReconciler) setStatus(
	ctx context.Context,
	cluster *aevrelv1.AevrelCluster,
	ready bool,
	version string,
	message string,
) error {
	lastSeen := cluster.Status.LastSeen
	if ready {
		lastSeen = time.Now().UTC().Format(time.RFC3339)
	}
	if cluster.Status.Ready == ready &&
		cluster.Status.Version == version &&
		cluster.Status.Message == message &&
		cluster.Status.LastSeen == lastSeen {
		return nil
	}
	cluster.Status.Ready = ready
	cluster.Status.Version = version
	cluster.Status.Message = message
	cluster.Status.LastSeen = lastSeen
	if err := r.Status().Update(ctx, cluster); err != nil {
		return err
	}
	return nil
}

func (r *ClusterHealthReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&aevrelv1.AevrelCluster{}).
		Complete(r)
}
