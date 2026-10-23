package main

import (
	"context"
	"flag"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/kueueadapter"
)

func main() {
	var metricsAddr string
	var probeAddr string
	var namespace string
	var workloadVersion string
	var syncInterval time.Duration
	var protectProgressGTE float64
	var requireCheckpointComplete bool
	var protectUnknown bool
	var strict bool
	var preemptionConfigName string
	var observationSource string
	var enableLeaderElection bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "metrics endpoint address")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "health probe address")
	flag.StringVar(&namespace, "namespace", "", "optional namespace to watch; empty watches all namespaces")
	flag.StringVar(&workloadVersion, "workload-api-version", kueueadapter.DefaultWorkloadGVK.Version, "Kueue Workload API version")
	flag.DurationVar(&syncInterval, "sync-interval", 30*time.Second, "checkpoint state refresh interval")
	flag.Float64Var(&protectProgressGTE, "protect-progress-gte", 0.8, "protect workloads at or above this normalized progress (0..1)")
	flag.BoolVar(&requireCheckpointComplete, "require-checkpoint-complete", true, "protect workloads without a complete checkpoint")
	flag.BoolVar(&protectUnknown, "protect-unknown", true, "protect workloads with unknown or invalid checkpoint state")
	flag.BoolVar(&strict, "strict", false, "require the configurable-only Kueue profile before starting")
	flag.StringVar(&preemptionConfigName, "preemption-config-name", "checkpoint-guard", "PreemptionConfig referenced by strict ClusterQueues")
	flag.StringVar(&observationSource, "observation-source", "auto", "checkpoint state source: auto, annotations, or checkpoint-record")
	flag.BoolVar(&enableLeaderElection, "leader-elect", true, "enable leader election")
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseDevMode(false)))
	policy := kueueadapter.Policy{
		ProtectProgressGTE:        protectProgressGTE,
		RequireCheckpointComplete: requireCheckpointComplete,
		ProtectUnknown:            protectUnknown,
	}
	if err := policy.Validate(); err != nil {
		ctrl.Log.Error(err, "invalid protection policy")
		os.Exit(1)
	}
	if syncInterval <= 0 {
		ctrl.Log.Error(nil, "sync-interval must be positive")
		os.Exit(1)
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		ctrl.Log.Error(err, "build Kubernetes scheme")
		os.Exit(1)
	}
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		ctrl.Log.Error(err, "build shardferry scheme")
		os.Exit(1)
	}
	workloadGVK := schema.GroupVersionKind{
		Group:   kueueadapter.KueueGroup,
		Version: workloadVersion,
		Kind:    "Workload",
	}
	kueueadapter.AddToScheme(scheme, workloadGVK)
	kueueadapter.AddToScheme(scheme, kueueadapter.ClusterQueueGVK)
	kueueadapter.AddToScheme(scheme, kueueadapter.PreemptionConfigGVK)

	managerOptions := ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "shardferry-kueue-adapter.shardferry.io",
	}
	if namespace != "" {
		managerOptions.Cache = cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				namespace: {},
			},
		}
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), managerOptions)
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}
	if strict {
		report, err := kueueadapter.CheckStrictProfile(
			context.Background(),
			mgr.GetAPIReader(),
			preemptionConfigName,
		)
		if err != nil {
			ctrl.Log.Error(err, "strict Kueue profile is not ready")
			os.Exit(1)
		}
		ctrl.Log.Info(
			"strict Kueue profile validated",
			"preemptionConfig", report.ConfigName,
			"clusterQueues", report.ClusterQueues,
		)
	}

	if err := (&kueueadapter.Reconciler{
		Client:          mgr.GetClient(),
		Policy:          policy,
		WorkloadGVK:     workloadGVK,
		RefreshInterval: syncInterval,
		Source:          observationSourceFor(mgr.GetClient(), observationSource),
		Projector: kueueadapter.WorkloadLabelProjector{
			Client:   mgr.GetClient(),
			Recorder: mgr.GetEventRecorderFor("shardferry-kueue-adapter"),
		},
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create Kueue adapter")
		os.Exit(1)
	}
	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to set health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		ctrl.Log.Error(err, "unable to set readiness check")
		os.Exit(1)
	}
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		ctrl.Log.Error(err, "manager exited")
		os.Exit(1)
	}
}

func observationSourceFor(
	reader client.Reader,
	value string,
) kueueadapter.ObservationSource {
	switch value {
	case "", "auto", "checkpoint-record":
		return kueueadapter.CheckpointRecordSource{Reader: reader}
	case "annotations":
		return nil
	default:
		ctrl.Log.Error(
			nil,
			"invalid observation-source",
			"value", value,
			"allowed", "auto, annotations, checkpoint-record",
		)
		os.Exit(1)
		return nil
	}
}
