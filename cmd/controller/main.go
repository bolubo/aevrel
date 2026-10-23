package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"flag"
	"net/http"
	"os"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/artifactstore"
	"github.com/shardferry/shardferry/internal/clusterclient"
	"github.com/shardferry/shardferry/internal/controller"
	"github.com/shardferry/shardferry/internal/execution"
	"github.com/shardferry/shardferry/internal/liveclient"
)

func main() {
	var metricsAddr string
	var probeAddr string
	var artifactAddr string
	var artifactPublicURL string
	var artifactDir string
	var agentCA string
	var agentCert string
	var agentKey string
	var artifactCert string
	var artifactKey string
	var artifactClientCA string
	var liveURL string
	var enableLeaderElection bool
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "metrics endpoint address")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "health probe address")
	flag.StringVar(&artifactAddr, "artifact-bind-address", ":8082", "artifact endpoint address")
	flag.StringVar(&artifactPublicURL, "artifact-public-url", "", "artifact URL visible to agents")
	flag.StringVar(&artifactDir, "artifact-dir", "/var/lib/shardferry/artifacts", "persistent artifact directory")
	flag.StringVar(&agentCA, "agent-ca", "", "CA for agent mTLS")
	flag.StringVar(&agentCert, "agent-cert", "", "client certificate for agent mTLS")
	flag.StringVar(&agentKey, "agent-key", "", "client key for agent mTLS")
	flag.StringVar(&artifactCert, "artifact-tls-cert", "", "artifact server TLS certificate")
	flag.StringVar(&artifactKey, "artifact-tls-key", "", "artifact server TLS key")
	flag.StringVar(&artifactClientCA, "artifact-tls-client-ca", "", "client CA for artifact mTLS")
	flag.StringVar(&liveURL, "live-url", "", "optional live-rank-split executor base URL")
	flag.BoolVar(&enableLeaderElection, "leader-elect", true, "enable leader election")
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseDevMode(false)))
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		os.Exit(1)
	}
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		os.Exit(1)
	}
	agentToken := os.Getenv("SHARDFERRY_AGENT_TOKEN")
	if agentToken == "" {
		ctrl.Log.Error(nil, "SHARDFERRY_AGENT_TOKEN is required for agent communication")
		os.Exit(1)
	}
	if artifactPublicURL == "" {
		ctrl.Log.Error(nil, "artifact-public-url is required")
		os.Exit(1)
	}
	artifacts, err := artifactstore.NewFileStore(artifactPublicURL, artifactDir)
	if err != nil {
		ctrl.Log.Error(err, "unable to open artifact store")
		os.Exit(1)
	}
	artifacts.SetToken(agentToken)
	artifactServer := &http.Server{
		Addr:    artifactAddr,
		Handler: artifacts.Handler(),
	}
	go func() {
		var serveErr error
		if artifactCert != "" || artifactKey != "" {
			if artifactCert == "" || artifactKey == "" {
				ctrl.Log.Error(nil, "artifact TLS cert and key are required together")
				return
			}
			certificate, loadErr := tls.LoadX509KeyPair(artifactCert, artifactKey)
			if loadErr != nil {
				ctrl.Log.Error(loadErr, "load artifact TLS certificate")
				return
			}
			artifactServer.TLSConfig = &tls.Config{
				MinVersion:   tls.VersionTLS12,
				Certificates: []tls.Certificate{certificate},
			}
			if artifactClientCA != "" {
				data, readErr := os.ReadFile(artifactClientCA)
				if readErr != nil {
					ctrl.Log.Error(readErr, "read artifact client CA")
					return
				}
				pool := x509.NewCertPool()
				if !pool.AppendCertsFromPEM(data) {
					ctrl.Log.Error(nil, "artifact client CA contains no certificates")
					return
				}
				artifactServer.TLSConfig.ClientCAs = pool
				artifactServer.TLSConfig.ClientAuth = tls.RequireAndVerifyClientCert
			}
			serveErr = artifactServer.ListenAndServeTLS("", "")
		} else {
			serveErr = artifactServer.ListenAndServe()
		}
		if serveErr != nil && serveErr != http.ErrServerClosed {
			ctrl.Log.Error(serveErr, "artifact server exited")
		}
	}()

	var agents *agentclient.Client
	if agentCert != "" || agentKey != "" || agentCA != "" {
		agents, err = agentclient.NewWithTLS(agentToken, agentCA, agentCert, agentKey)
		if err != nil {
			ctrl.Log.Error(err, "configure agent TLS")
			os.Exit(1)
		}
	} else {
		agents = agentclient.New(agentToken)
	}
	runner := execution.NewRunner(agents, 16)
	runner.SetArtifacts(artifacts)
	runner.SetProofMerger(agents)

	var live controller.LiveRankSplitExecutor
	if liveURL != "" {
		liveToken := os.Getenv("SHARDFERRY_LIVE_TOKEN")
		if liveToken == "" {
			liveToken = agentToken
		}
		if agentCert != "" || agentKey != "" || agentCA != "" {
			live, err = liveclient.NewWithTLS(
				liveURL,
				liveToken,
				agentCA,
				agentCert,
				agentKey,
			)
		} else {
			live = liveclient.New(liveURL, liveToken)
		}
		if err != nil {
			ctrl.Log.Error(err, "configure live-rank-split client")
			os.Exit(1)
		}
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsAddr},
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "shardferry-controller.shardferry.io",
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to create manager")
		os.Exit(1)
	}

	orchestrator := &controller.Orchestrator{
		Agents:    agents,
		Tasks:     agents,
		Proofs:    agents,
		Artifacts: artifacts,
	}
	preparer := controller.NewPreparationRunner(
		mgr.GetClient(),
		orchestrator,
		runner,
		4,
	)
	clusterClient := clusterclient.New(agentToken)
	if agentCA != "" || agentCert != "" || agentKey != "" {
		clusterClient, err = clusterclient.NewWithTLS(
			agentToken,
			agentCA,
			agentCert,
			agentKey,
		)
		if err != nil {
			ctrl.Log.Error(err, "configure cluster gateway TLS")
			os.Exit(1)
		}
	}
	resolver := &controller.AgentResolver{
		Client:   mgr.GetClient(),
		Reader:   mgr.GetAPIReader(),
		Agents:   agents,
		Clusters: clusterClient,
		Tokens:   agents,
	}
	preparer.SetResolver(resolver)
	if err := mgr.Add(preparer); err != nil {
		ctrl.Log.Error(err, "unable to add preparation runner")
		os.Exit(1)
	}
	if err := mgr.Add(&managedRunner{runner: runner}); err != nil {
		ctrl.Log.Error(err, "unable to add execution runner")
		os.Exit(1)
	}
	if err := (&controller.Reconciler{
		Client:       mgr.GetClient(),
		Scheme:       mgr.GetScheme(),
		Runner:       runner,
		Preparer:     preparer,
		Resolver:     resolver,
		Cleaner:      agents,
		Artifacts:    artifacts,
		Orchestrator: orchestrator,
		Live:         live,
		Recorder:     mgr.GetEventRecorderFor("shardferry-controller"),
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create controller")
		os.Exit(1)
	}
	if err := (&controller.CheckpointReadinessReconciler{
		Client:     mgr.GetClient(),
		Scheme:     mgr.GetScheme(),
		AgentToken: agentToken,
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create checkpoint readiness controller")
		os.Exit(1)
	}
	if err := (&controller.RestoreDrillReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create restore drill controller")
		os.Exit(1)
	}
	if err := (&controller.RescueDecisionReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create rescue decision controller")
		os.Exit(1)
	}
	if err := (&controller.RescueEventReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create rescue event controller")
		os.Exit(1)
	}
	if err := (&controller.ClusterHealthReconciler{
		Client:   mgr.GetClient(),
		Reader:   mgr.GetAPIReader(),
		Clusters: clusterClient,
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to create cluster health controller")
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

type managedRunner struct {
	runner *execution.Runner
}

func (m *managedRunner) Start(ctx context.Context) error {
	m.runner.Start(ctx)
	<-ctx.Done()
	m.runner.Stop()
	return nil
}

func (m *managedRunner) NeedLeaderElection() bool {
	return true
}
