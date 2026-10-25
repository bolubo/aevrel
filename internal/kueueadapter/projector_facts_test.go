package kueueadapter

import (
	"context"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func factWorkload() *unstructured.Unstructured {
	workload := &unstructured.Unstructured{}
	workload.SetGroupVersionKind(DefaultWorkloadGVK)
	workload.SetName("training-workload")
	workload.SetNamespace("default")
	return workload
}

func TestProjectFactsWritesTheEnvelope(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	changed, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	)
	if err != nil {
		t.Fatal(err)
	}
	if !changed {
		t.Fatal("expected the envelope to be written")
	}
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelEnvelopeSchemaVersion, SchemaLabelValue)
	assertLabel(t, got, LabelEnvelopeComplete, "1")
	assertLabel(t, got, LabelEnvelopeFresh, "1")
	assertLabel(t, got, LabelEnvelopeLoadable, "1")
	assertLabel(t, got, LabelEnvelopeMovable, FactValueUnknown)
	assertLabel(t, got, LabelEnvelopeSource, FactSourceCheckpointRecord)
	assertLabel(t, got, LabelEnvelopeRestoreMode, "restore")
	assertLabel(t, got, LabelEnvelopeEstimatedRTO, "120")
	assertLabel(t, got, LabelEnvelopeObservedAt, "20261025T120000Z")
	assertLabel(t, got, LabelEnvelopeConformance, "valid")
}

func TestProjectFactsNeverTouchesTheProtectionDecision(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	); err != nil {
		t.Fatal(err)
	}
	got := getWorkload(t, kubeClient, workload)
	assertMissingLabel(t, got, LabelPreemptionProtected)
	assertMissingLabel(t, got, LabelPreemptionReason)
	assertMissingLabel(t, got, LabelDecisionSource)
	assertMissingLabel(t, got, LabelCheckpointComplete)
}

func TestProjectFactsIsDisabledByDefault(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{Client: kubeClient}
	changed, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	)
	if err != nil {
		t.Fatal(err)
	}
	if changed {
		t.Fatal("the envelope must not be published without an explicit opt-in")
	}
	assertNoEnvelopeLabels(t, getWorkload(t, kubeClient, workload))
}

func TestProjectFactsRecomputableFromTheRecord(t *testing.T) {
	// The envelope must always be derivable from authoritative readiness. This
	// test recomputes every expected label from the record itself rather than
	// from the fact set the projector received.
	record := factTestRecord()
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:           aevrelv1.ReadinessReady,
		Complete:         true,
		Fresh:            true,
		Loadable:         factBoolPtr(true),
		ObservedAt:       factTestObservedAt,
		LastDrill:        "default/training-drill",
		LastDrillVersion: "step-100",
		LastDrillDigest:  "sha256:abc",
	}
	facts := CheckpointFactsForRecord(
		record,
		1,
		record.Name,
		FactsOptions{Now: factTestNow()},
	)

	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		facts,
	); err != nil {
		t.Fatal(err)
	}

	readiness := record.Status.Readiness
	got := getWorkload(t, kubeClient, workload)
	assertLabel(t, got, LabelEnvelopeComplete, boolLabel(readiness.Complete))
	assertLabel(t, got, LabelEnvelopeFresh, boolLabel(readiness.Fresh))
	assertLabel(t, got, LabelEnvelopeLoadable, boolLabel(*readiness.Loadable))
	assertLabel(t, got, LabelEnvelopeMovable, FactValueUnknown)
	assertLabel(t, got, LabelEnvelopeSource, FactSourceCheckpointRecord)
	assertLabel(t, got, LabelEnvelopeRestoreMode, "restore")
	assertLabel(t, got, LabelEnvelopeEstimatedRTO, FactValueUnknown)
	observed, err := time.Parse(time.RFC3339, readiness.ObservedAt)
	if err != nil {
		t.Fatal(err)
	}
	assertLabel(
		t,
		got,
		LabelEnvelopeObservedAt,
		observed.UTC().Format(EnvelopeTimeLayout),
	)
}

func TestProjectFactsWithdrawsAnInvalidEnvelope(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	); err != nil {
		t.Fatal(err)
	}
	assertLabel(t, getWorkload(t, kubeClient, workload), LabelEnvelopeLoadable, "1")

	invalid := CheckpointFactsForAmbiguity(2, FactsOptions{})
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		invalid,
	); err != nil {
		t.Fatal(err)
	}
	assertNoEnvelopeLabels(t, getWorkload(t, kubeClient, workload))
}

func TestClearFactsRemovesTheEnvelope(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	); err != nil {
		t.Fatal(err)
	}
	if err := projector.ClearFacts(context.Background(), workload); err != nil {
		t.Fatal(err)
	}
	assertNoEnvelopeLabels(t, getWorkload(t, kubeClient, workload))
}

func TestReconcilerPublishesTheEnvelopeWithoutChangingTheDecision(t *testing.T) {
	job, _ := sourceWorkload(map[string]string{
		AnnotationProtection: "enabled",
	})
	workload := testWorkload(job)
	record := sourceRecord(job, "step-100")
	record.Status.Readiness = aevrelv1.CheckpointReadinessStatus{
		Status:           aevrelv1.ReadinessReady,
		Complete:         true,
		Fresh:            true,
		Loadable:         factBoolPtr(true),
		LastDrill:        "default/training-drill",
		LastDrillVersion: "step-100",
		LastDrillDigest:  "sha256:abc",
		ObservedAt:       time.Now().UTC().Format(time.RFC3339),
	}
	kubeClient := sourceClient(t, job, workload, record)
	reconciler := testReconciler(kubeClient, DefaultPolicy())
	reconciler.Source = CheckpointRecordSource{Reader: kubeClient}
	reconciler.Projector = WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := reconciler.Reconcile(
		context.Background(),
		workloadRequest(workload),
	); err != nil {
		t.Fatal(err)
	}

	got := getWorkload(t, kubeClient, workload)
	// The binary decision is unchanged by the envelope.
	assertLabel(t, got, LabelPreemptionProtected, "0")
	assertLabel(t, got, LabelPreemptionReason, string(ReasonEligible))
	assertLabel(t, got, LabelDecisionSource, "checkpoint-record")
	// The envelope is present and consistent with it.
	assertLabel(t, got, LabelEnvelopeSchemaVersion, SchemaLabelValue)
	assertLabel(t, got, LabelEnvelopeLoadable, "1")
	assertLabel(t, got, LabelEnvelopeRestoreMode, "restore")
	assertLabel(t, got, LabelEnvelopeConformance, "valid")
}

func TestReconcilerClearsTheEnvelopeWhenProtectionIsDisabled(t *testing.T) {
	job, workload := sourceWorkload(map[string]string{})
	kubeClient := sourceClient(t, job, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		validRecordFacts(),
	); err != nil {
		t.Fatal(err)
	}
	reconciler := testReconciler(kubeClient, DefaultPolicy())
	reconciler.Projector = projector
	if _, err := reconciler.Reconcile(
		context.Background(),
		workloadRequest(workload),
	); err != nil {
		t.Fatal(err)
	}
	assertNoEnvelopeLabels(t, getWorkload(t, kubeClient, workload))
}

func TestProjectFactsCarriesReasonCodesVerbatim(t *testing.T) {
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	facts := validRecordFacts()
	facts.ReasonCodes = []string{"CHECKPOINT_COMPLETE", "DRILL_VERIFIED"}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		facts,
	); err != nil {
		t.Fatal(err)
	}
	assertLabel(
		t,
		getWorkload(t, kubeClient, workload),
		LabelEnvelopeReasonCodes,
		"CHECKPOINT_COMPLETE_DRILL_VERIFIED",
	)
}

func TestProjectFactsNeverTruncatesReasonCodes(t *testing.T) {
	// A Kubernetes label value holds at most 63 characters. A code list that
	// does not fit must degrade to "unknown", because a truncated list would
	// read as the complete list and a consumer would never go look at the
	// record that holds the authoritative reasons.
	workload := factWorkload()
	kubeClient := sourceClient(t, workload)
	projector := WorkloadLabelProjector{
		Client:         kubeClient,
		EnvelopeLabels: true,
	}
	facts := validRecordFacts()
	facts.ReasonCodes = []string{
		"CHECKPOINT_COMPLETE",
		"DRILL_VERIFIED",
		"TARGET_WORLD_SIZE_COMPATIBLE",
		"ARTIFACT_DIGEST_PINNED",
	}
	if _, err := projector.ProjectFacts(
		context.Background(),
		workload,
		facts,
	); err != nil {
		t.Fatal(err)
	}
	assertLabel(
		t,
		getWorkload(t, kubeClient, workload),
		LabelEnvelopeReasonCodes,
		FactValueUnknown,
	)
}

func assertNoEnvelopeLabels(
	t *testing.T,
	object *unstructured.Unstructured,
) {
	t.Helper()
	for _, key := range EnvelopeLabels {
		assertMissingLabel(t, object, key)
	}
}
