package main

import (
	"flag"
	"fmt"
	"net/http"
	"os"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	"github.com/shardferry/shardferry/internal/agentclient"
	"github.com/shardferry/shardferry/internal/gateway"
)

func main() {
	var bindAddr string
	var publicURL string
	var clusterName string
	var namespace string
	var version string
	flag.StringVar(&bindAddr, "bind-address", ":9445", "gateway listen address")
	flag.StringVar(&publicURL, "public-url", "", "URL controller uses to reach this gateway")
	flag.StringVar(&clusterName, "cluster-name", "", "cluster name reported to the controller")
	flag.StringVar(&namespace, "namespace", "default", "default namespace for agent discovery")
	flag.StringVar(&version, "version", "dev", "gateway build version")
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseDevMode(false)))
	gatewayToken := os.Getenv("SHARDFERRY_GATEWAY_TOKEN")
	agentToken := os.Getenv("SHARDFERRY_AGENT_TOKEN")
	if gatewayToken == "" || agentToken == "" {
		ctrl.Log.Error(
			nil,
			"SHARDFERRY_GATEWAY_TOKEN and SHARDFERRY_AGENT_TOKEN are required",
		)
		os.Exit(1)
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		ctrl.Log.Error(err, "build Kubernetes scheme")
		os.Exit(1)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		ctrl.Log.Error(err, "add core API to scheme")
		os.Exit(1)
	}
	kubeClient, err := client.New(ctrl.GetConfigOrDie(), client.Options{Scheme: scheme})
	if err != nil {
		ctrl.Log.Error(err, "create Kubernetes client")
		os.Exit(1)
	}
	server, err := gateway.New(gateway.Config{
		PublicURL:    publicURL,
		ClusterName:  clusterName,
		Version:      version,
		GatewayToken: gatewayToken,
		AgentToken:   agentToken,
		AgentClient:  agentclient.New(agentToken),
		Client:       kubeClient,
		Namespace:    namespace,
	})
	if err != nil {
		ctrl.Log.Error(err, "configure gateway")
		os.Exit(1)
	}
	httpServer := &http.Server{Addr: bindAddr, Handler: server.Handler()}
	ctrl.Log.Info("starting gateway", "address", bindAddr, "cluster", clusterName)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		ctrl.Log.Error(err, "gateway exited")
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
