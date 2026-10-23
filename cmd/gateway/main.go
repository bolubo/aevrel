package main

import (
	"crypto/tls"
	"crypto/x509"
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

	"github.com/bolubo/aevrel/internal/agentclient"
	"github.com/bolubo/aevrel/internal/gateway"
)

func main() {
	var bindAddr string
	var publicURL string
	var clusterName string
	var namespace string
	var version string
	var tlsCert string
	var tlsKey string
	var tlsClientCA string
	var agentCA string
	var agentCert string
	var agentKey string
	flag.StringVar(&bindAddr, "bind-address", ":9445", "gateway listen address")
	flag.StringVar(&publicURL, "public-url", "", "URL controller uses to reach this gateway")
	flag.StringVar(&clusterName, "cluster-name", "", "cluster name reported to the controller")
	flag.StringVar(&namespace, "namespace", "default", "default namespace for agent discovery")
	flag.StringVar(&version, "version", "dev", "gateway build version")
	flag.StringVar(&tlsCert, "tls-cert", "", "gateway server TLS certificate")
	flag.StringVar(&tlsKey, "tls-key", "", "gateway server TLS key")
	flag.StringVar(&tlsClientCA, "tls-client-ca", "", "client CA for gateway mTLS")
	flag.StringVar(&agentCA, "agent-ca", "", "CA for local agent mTLS")
	flag.StringVar(&agentCert, "agent-cert", "", "client certificate for local agent mTLS")
	flag.StringVar(&agentKey, "agent-key", "", "client key for local agent mTLS")
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseDevMode(false)))
	gatewayToken := os.Getenv("AEVREL_GATEWAY_TOKEN")
	agentToken := os.Getenv("AEVREL_AGENT_TOKEN")
	if gatewayToken == "" || agentToken == "" {
		ctrl.Log.Error(
			nil,
			"AEVREL_GATEWAY_TOKEN and AEVREL_AGENT_TOKEN are required",
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
	agentClient := agentclient.New(agentToken)
	if agentCA != "" || agentCert != "" || agentKey != "" {
		agentClient, err = agentclient.NewWithTLS(
			agentToken,
			agentCA,
			agentCert,
			agentKey,
		)
		if err != nil {
			ctrl.Log.Error(err, "configure gateway agent TLS")
			os.Exit(1)
		}
	}
	server, err := gateway.New(gateway.Config{
		PublicURL:         publicURL,
		ClusterName:       clusterName,
		Version:           version,
		GatewayToken:      gatewayToken,
		AgentToken:        agentToken,
		RequireClientCert: tlsClientCA != "",
		AgentClient:       agentClient,
		AgentTransport:    agentClient.HTTP.Transport,
		Client:            kubeClient,
		Namespace:         namespace,
	})
	if err != nil {
		ctrl.Log.Error(err, "configure gateway")
		os.Exit(1)
	}
	httpServer := &http.Server{Addr: bindAddr, Handler: server.Handler()}
	ctrl.Log.Info("starting gateway", "address", bindAddr, "cluster", clusterName)
	var serveErr error
	if tlsCert != "" || tlsKey != "" {
		if tlsCert == "" || tlsKey == "" {
			ctrl.Log.Error(nil, "gateway TLS cert and key are required together")
			os.Exit(1)
		}
		certificate, err := tls.LoadX509KeyPair(tlsCert, tlsKey)
		if err != nil {
			ctrl.Log.Error(err, "load gateway TLS certificate")
			os.Exit(1)
		}
		httpServer.TLSConfig = &tls.Config{
			MinVersion:   tls.VersionTLS12,
			Certificates: []tls.Certificate{certificate},
		}
		if tlsClientCA != "" {
			data, err := os.ReadFile(tlsClientCA)
			if err != nil {
				ctrl.Log.Error(err, "read gateway client CA")
				os.Exit(1)
			}
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(data) {
				ctrl.Log.Error(nil, "gateway client CA contains no certificates")
				os.Exit(1)
			}
			httpServer.TLSConfig.ClientCAs = pool
			httpServer.TLSConfig.ClientAuth = tls.VerifyClientCertIfGiven
		}
		serveErr = httpServer.ListenAndServeTLS("", "")
	} else {
		serveErr = httpServer.ListenAndServe()
	}
	if serveErr != nil && serveErr != http.ErrServerClosed {
		ctrl.Log.Error(serveErr, "gateway exited")
		fmt.Fprintln(os.Stderr, serveErr)
		os.Exit(1)
	}
}
