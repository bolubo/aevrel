package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"reflect"
	"sort"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/control"
)

const rescueDecisionPollInterval = 30 * time.Second

type RescueDecisionReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

func (r *RescueDecisionReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var decision aevrelv1.RescueDecision
	if err := r.Get(ctx, request.NamespacedName, &decision); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if decision.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	if strings.TrimSpace(decision.Spec.Event) == "" {
		return r.writeRescueDecision(
			ctx,
			&decision,
			control.RescueDecision{
				Action:    control.RescueHold,
				Code:      "INVALID_SPEC",
				Reason:    "event is required",
				DecidedAt: time.Now().UTC().Format(time.RFC3339),
			},
			false,
		)
	}
	fallback := decision.Spec.Fallback
	if fallback == "" {
		fallback = "hold"
	}
	if fallback != "hold" && fallback != "restart" {
		return r.writeRescueDecision(
			ctx,
			&decision,
			control.RescueDecision{
				Action:    control.RescueHold,
				Code:      "INVALID_SPEC",
				Reason:    "fallback must be hold or restart",
				DecidedAt: time.Now().UTC().Format(time.RFC3339),
			},
			false,
		)
	}

	readiness, ambiguous, err := r.rescueReadiness(ctx, &decision)
	if err != nil {
		return ctrl.Result{}, err
	}
	if ambiguous {
		return r.writeRescueDecision(
			ctx,
			&decision,
			control.RescueDecision{
				Event:     decision.Spec.Event,
				Action:    control.RescueHold,
				Code:      "CHECKPOINT_AMBIGUOUS",
				Reason:    "workloadRef matches more than one CheckpointRecord",
				Target:    decision.Spec.Target,
				DecidedAt: time.Now().UTC().Format(time.RFC3339),
			},
			true,
		)
	}
	result := control.DecideRescue(
		decision.Spec.Event,
		decision.Spec.Target,
		fallback,
		readiness,
		time.Now(),
	)
	updated, err := r.writeRescueDecision(ctx, &decision, result, true)
	if err != nil {
		return ctrl.Result{}, err
	}
	if result.Action == control.RescueHold {
		updated.RequeueAfter = rescueDecisionPollInterval
	}
	return updated, nil
}

func (r *RescueDecisionReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&aevrelv1.RescueDecision{}).
		Complete(r)
}

func (r *RescueDecisionReconciler) rescueReadiness(
	ctx context.Context,
	decision *aevrelv1.RescueDecision,
) (*control.CheckpointReadiness, bool, error) {
	if decision.Spec.CheckpointRef != "" {
		var record aevrelv1.CheckpointRecord
		err := r.Get(ctx, client.ObjectKey{
			Namespace: decision.Namespace,
			Name:      decision.Spec.CheckpointRef,
		}, &record)
		if apierrors.IsNotFound(err) {
			return nil, false, nil
		}
		if err != nil {
			return nil, false, err
		}
		return rescueReadinessFromRecord(&record), false, nil
	}
	if decision.Spec.WorkloadRef == "" {
		return nil, false, nil
	}
	var records aevrelv1.CheckpointRecordList
	if err := r.List(
		ctx,
		&records,
		client.InNamespace(decision.Namespace),
	); err != nil {
		return nil, false, err
	}
	matches := make([]aevrelv1.CheckpointRecord, 0)
	for _, record := range records.Items {
		if record.Spec.JobRef == decision.Spec.WorkloadRef {
			matches = append(matches, record)
		}
	}
	switch len(matches) {
	case 0:
		return nil, false, nil
	case 1:
		return rescueReadinessFromRecord(&matches[0]), false, nil
	default:
		sort.Slice(matches, func(i, j int) bool {
			return matches[i].Name < matches[j].Name
		})
		return nil, true, nil
	}
}

func rescueReadinessFromRecord(
	record *aevrelv1.CheckpointRecord,
) *control.CheckpointReadiness {
	status := record.Status.Readiness
	return &control.CheckpointReadiness{
		Status:     control.ReadinessStatus(status.Status),
		Complete:   status.Complete,
		Fresh:      status.Fresh,
		Loadable:   status.Loadable,
		Movable:    status.Movable,
		ObservedAt: status.ObservedAt,
		LastDrill:  status.LastDrill,
	}
}

func (r *RescueDecisionReconciler) writeRescueDecision(
	ctx context.Context,
	current *aevrelv1.RescueDecision,
	decision control.RescueDecision,
	found bool,
) (ctrl.Result, error) {
	before := current.Status.DeepCopy()
	if current.Status.Action == string(decision.Action) &&
		current.Status.ReasonCode == decision.Code &&
		current.Status.Reason == decision.Reason &&
		current.Status.Target == decision.Target &&
		current.Status.ProofRef == decision.ProofRef {
		decision.DecidedAt = current.Status.DecidedAt
	}
	current.Status.Action = string(decision.Action)
	current.Status.ReasonCode = decision.Code
	current.Status.Reason = decision.Reason
	current.Status.Target = decision.Target
	current.Status.ProofRef = decision.ProofRef
	current.Status.DecidedAt = decision.DecidedAt
	conditionStatus := metav1.ConditionTrue
	if !found {
		conditionStatus = metav1.ConditionFalse
	}
	meta.SetStatusCondition(&current.Status.Conditions, metav1.Condition{
		Type:               "RescueDecided",
		Status:             conditionStatus,
		Reason:             decision.Code,
		Message:            decision.Reason,
		ObservedGeneration: current.Generation,
	})
	if reflect.DeepEqual(before, &current.Status) {
		return ctrl.Result{}, nil
	}
	if err := r.Status().Update(ctx, current); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

type RescueEventReconciler struct {
	client.Client
	Scheme *runtime.Scheme
}

var rescueEventReasons = map[string]struct{}{
	"BackoffLimitExceeded": {},
	"DeadlineExceeded":     {},
	"Evicted":              {},
	"Failed":               {},
	"FailedScheduling":     {},
	"Killing":              {},
	"NodeNotReady":         {},
	"OOMKilled":            {},
	"Preempted":            {},
}

func (r *RescueEventReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var event corev1.Event
	if err := r.Get(ctx, request.NamespacedName, &event); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if event.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}
	if event.Type != corev1.EventTypeWarning {
		return ctrl.Result{}, nil
	}
	if _, ok := rescueEventReasons[event.Reason]; !ok {
		return ctrl.Result{}, nil
	}
	namespace := event.InvolvedObject.Namespace
	if namespace == "" {
		namespace = event.Namespace
	}
	if namespace == "" {
		return ctrl.Result{}, nil
	}

	workloadName, workloadObject, err := r.rescueWorkload(
		ctx,
		namespace,
		event.InvolvedObject,
	)
	if err != nil {
		return ctrl.Result{}, err
	}
	if workloadName == "" || workloadObject == nil {
		return ctrl.Result{}, nil
	}
	if !rescueOptedIn(workloadObject) {
		return ctrl.Result{}, nil
	}
	checkpointRef := ""
	annotations := workloadObject.GetAnnotations()
	if annotations != nil {
		checkpointRef = annotations["aevrel.dev/checkpoint-record"]
	}
	name := rescueDecisionName(&event, workloadName)
	decision := &aevrelv1.RescueDecision{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				"aevrel.dev/rescue-source": "kubernetes-event",
			},
			Annotations: map[string]string{
				"aevrel.dev/source-event": event.Name,
				"aevrel.dev/event-reason": event.Reason,
			},
		},
		Spec: aevrelv1.RescueDecisionSpec{
			Event:         event.Reason,
			WorkloadRef:   workloadName,
			CheckpointRef: checkpointRef,
			Fallback:      "hold",
		},
	}
	if err := r.Create(ctx, decision); err != nil &&
		!apierrors.IsAlreadyExists(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (r *RescueEventReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Event{}).
		Complete(r)
}

func (r *RescueEventReconciler) rescueWorkload(
	ctx context.Context,
	namespace string,
	reference corev1.ObjectReference,
) (string, client.Object, error) {
	switch reference.Kind {
	case "Job":
		var job batchv1.Job
		if err := r.Get(ctx, client.ObjectKey{
			Namespace: namespace,
			Name:      reference.Name,
		}, &job); err != nil {
			if apierrors.IsNotFound(err) {
				return "", nil, nil
			}
			return "", nil, err
		}
		return job.Name, &job, nil
	case "Pod":
		var pod corev1.Pod
		if err := r.Get(ctx, client.ObjectKey{
			Namespace: namespace,
			Name:      reference.Name,
		}, &pod); err != nil {
			if apierrors.IsNotFound(err) {
				return "", nil, nil
			}
			return "", nil, err
		}
		for _, owner := range pod.OwnerReferences {
			if owner.Kind != "Job" {
				continue
			}
			var job batchv1.Job
			if err := r.Get(ctx, client.ObjectKey{
				Namespace: namespace,
				Name:      owner.Name,
			}, &job); err == nil {
				if rescueOptedIn(&pod) || rescueOptedIn(&job) {
					if pod.Annotations == nil {
						pod.Annotations = map[string]string{}
					}
					if ref := job.Annotations["aevrel.dev/checkpoint-record"]; ref != "" &&
						pod.Annotations["aevrel.dev/checkpoint-record"] == "" {
						pod.Annotations["aevrel.dev/checkpoint-record"] = ref
					}
					return job.Name, &pod, nil
				}
			} else if !apierrors.IsNotFound(err) {
				return "", nil, err
			}
			if rescueOptedIn(&pod) {
				return owner.Name, &pod, nil
			}
		}
		if rescueOptedIn(&pod) {
			return pod.Name, &pod, nil
		}
		return "", nil, nil
	default:
		return "", nil, nil
	}
}

func rescueOptedIn(object client.Object) bool {
	if object == nil {
		return false
	}
	if object.GetLabels()["aevrel.dev/rescue"] == "enabled" {
		return true
	}
	annotations := object.GetAnnotations()
	if annotations["aevrel.dev/rescue"] == "enabled" ||
		annotations["aevrel.dev/checkpoint-record"] != "" {
		return true
	}
	for _, owner := range object.GetOwnerReferences() {
		if owner.APIVersion == aevrelv1.GroupVersion.String() &&
			owner.Kind == "CheckpointMigration" {
			return true
		}
	}
	return false
}

func rescueDecisionName(event *corev1.Event, workloadName string) string {
	key := strings.Join([]string{
		event.Namespace,
		event.InvolvedObject.Kind,
		event.InvolvedObject.Name,
		string(event.InvolvedObject.UID),
		event.Reason,
		string(event.UID),
	}, "\x00")
	sum := sha256.Sum256([]byte(key))
	hash := hex.EncodeToString(sum[:6])
	base := sanitizeRescueName(workloadName)
	maxBase := 63 - len(hash) - len("rescue--")
	if len(base) > maxBase {
		base = strings.TrimRight(base[:maxBase], "-")
	}
	if base == "" {
		base = "workload"
	}
	return "rescue-" + base + "-" + hash
}

func sanitizeRescueName(value string) string {
	value = strings.ToLower(value)
	var builder strings.Builder
	lastDash := false
	for _, character := range value {
		valid := character >= 'a' && character <= 'z' ||
			character >= '0' && character <= '9'
		if valid {
			builder.WriteRune(character)
			lastDash = false
			continue
		}
		if !lastDash && builder.Len() > 0 {
			builder.WriteByte('-')
			lastDash = true
		}
	}
	return strings.Trim(builder.String(), "-")
}
