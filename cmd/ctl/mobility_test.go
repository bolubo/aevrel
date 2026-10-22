package main

import (
	"bytes"
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	shardferryv1 "github.com/shardferry/shardferry/api/v1"
)

func TestExecuteGetMobilityTransaction(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := shardferryv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	transaction := &shardferryv1.MobilityTransaction{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "training-move-view",
			Namespace: "default",
		},
		Spec: shardferryv1.MobilityTransactionSpec{
			RecordRef:    "training-step-100",
			MigrationRef: "training-move",
			Mode:         shardferryv1.TransactionModeStatic,
			Source:       "cluster-a",
			Target:       "cluster-b",
		},
		Status: shardferryv1.MobilityTransactionStatus{
			Phase:          shardferryv1.TransactionSucceeded,
			MigrationPhase: shardferryv1.MigrationPhaseSucceeded,
			ProofRef:       "proof://move",
		},
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(transaction).
		Build()
	var out bytes.Buffer
	if err := executeGetMobilityTransaction(
		context.Background(),
		kubeClient,
		"default",
		"training-move-view",
		false,
		&out,
	); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	for _, wanted := range []string{
		"Phase:       Succeeded",
		"Migration:   training-move",
		"Proof:       proof://move",
	} {
		if !strings.Contains(text, wanted) {
			t.Fatalf("output missing %q:\n%s", wanted, text)
		}
	}
}
