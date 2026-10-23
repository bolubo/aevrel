package controller

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	k8svalidation "k8s.io/apimachinery/pkg/util/validation"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/targetready"
)

const (
	targetReadyReportPath   = "/dev/termination-log"
	targetReadyCondition    = "TargetReady"
	targetReadyJobNameLimit = 63
)

type targetValidationSettings struct {
	Version        string
	Steps          int32
	Timeout        time.Duration
	JobName        string
	HasJobTemplate bool
	StateTemplate  string
	StateProviders string
	RequireState   bool
}

func parseTargetValidationSettings(
	migration *aevrelv1.CheckpointMigration,
) (targetValidationSettings, bool, error) {
	spec := migration.Spec.Target.Validation
	if spec == nil {
		return targetValidationSettings{}, false, nil
	}
	if strings.TrimSpace(spec.Version) == "" {
		return targetValidationSettings{}, true, fmt.Errorf(
			"target.validation.version is required",
		)
	}
	hasTemplate := spec.JobTemplate != nil
	hasJobName := strings.TrimSpace(spec.JobName) != ""
	if hasTemplate == hasJobName {
		return targetValidationSettings{}, true, fmt.Errorf(
			"exactly one of target.validation.jobTemplate or jobName is required",
		)
	}
	if hasJobName {
		if errors := k8svalidation.IsDNS1123Subdomain(spec.JobName); len(errors) > 0 {
			return targetValidationSettings{}, true, fmt.Errorf(
				"invalid target.validation.jobName: %s",
				strings.Join(errors, "; "),
			)
		}
	}
	steps := spec.Steps
	if steps <= 0 {
		steps = 10
	}
	timeoutSeconds := spec.TimeoutSeconds
	if timeoutSeconds <= 0 {
		timeoutSeconds = 600
	}
	stateTemplate := ""
	stateProviders := ""
	requireState := false
	if migration.Spec.Live != nil && liveRequiresState(migration) {
		stateTemplate = migration.Spec.Live.StateRoot
		stateProviders = strings.Join(
			migration.Spec.Live.StateProviders,
			",",
		)
		requireState = true
	}
	return targetValidationSettings{
		Version:        spec.Version,
		Steps:          steps,
		Timeout:        time.Duration(timeoutSeconds) * time.Second,
		JobName:        spec.JobName,
		HasJobTemplate: hasTemplate,
		StateTemplate:  stateTemplate,
		StateProviders: stateProviders,
		RequireState:   requireState,
	}, true, nil
}

func renderTargetValidationJob(
	migration *aevrelv1.CheckpointMigration,
	settings targetValidationSettings,
) (*batchv1.Job, error) {
	raw := migration.Spec.Target.Validation.JobTemplate
	payload, err := rawExtensionPayload(raw)
	if err != nil {
		return nil, err
	}
	var job batchv1.Job
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&job); err != nil {
		return nil, fmt.Errorf("decode target.validation.jobTemplate: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, fmt.Errorf("target.validation.jobTemplate contains trailing data")
	}
	if job.APIVersion != "batch/v1" || job.Kind != "Job" {
		return nil, fmt.Errorf(
			"target.validation.jobTemplate must be batch/v1 Job, got %s %s",
			job.APIVersion,
			job.Kind,
		)
	}
	if job.Namespace != "" && job.Namespace != migration.Namespace {
		return nil, fmt.Errorf(
			"target validation Job namespace %q must match migration namespace %q",
			job.Namespace,
			migration.Namespace,
		)
	}
	if len(job.Spec.Template.Spec.Containers) == 0 {
		return nil, fmt.Errorf("target validation Job has no containers")
	}
	if job.Spec.Suspend != nil && *job.Spec.Suspend {
		return nil, fmt.Errorf("target validation Job must not be suspended")
	}
	indexed := batchv1.IndexedCompletion
	if job.Spec.CompletionMode != nil && *job.Spec.CompletionMode != indexed {
		return nil, fmt.Errorf(
			"target validation Job must use Indexed completion mode",
		)
	}
	if job.Spec.Completions != nil &&
		*job.Spec.Completions != migration.Spec.Target.WorldSize {
		return nil, fmt.Errorf(
			"target validation Job completions %d do not match target worldSize %d",
			*job.Spec.Completions,
			migration.Spec.Target.WorldSize,
		)
	}
	if job.Spec.Parallelism != nil &&
		*job.Spec.Parallelism != migration.Spec.Target.WorldSize {
		return nil, fmt.Errorf(
			"target validation Job parallelism %d does not match target worldSize %d",
			*job.Spec.Parallelism,
			migration.Spec.Target.WorldSize,
		)
	}

	job.TypeMeta = metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"}
	job.Namespace = migration.Namespace
	job.Name = targetValidationJobName(migration)
	job.GenerateName = ""
	job.Spec.CompletionMode = &indexed
	job.Spec.Completions = int32Pointer(migration.Spec.Target.WorldSize)
	job.Spec.Parallelism = int32Pointer(migration.Spec.Target.WorldSize)
	job.Spec.BackoffLimit = int32Pointer(0)
	job.Spec.TTLSecondsAfterFinished = nil
	activeDeadline := int64(settings.Timeout.Seconds())
	job.Spec.ActiveDeadlineSeconds = &activeDeadline
	job.Spec.Template.Spec.RestartPolicy = corev1.RestartPolicyNever
	if job.Labels == nil {
		job.Labels = map[string]string{}
	}
	job.Labels["app.kubernetes.io/managed-by"] = "aevrel"
	job.Labels["aevrel.dev/target-ready"] = "true"
	if job.Annotations == nil {
		job.Annotations = map[string]string{}
	}
	job.Annotations["aevrel.dev/migration-uid"] = migrationUID(migration)
	job.Annotations["aevrel.dev/target-version"] = settings.Version
	if migration.UID != "" {
		controller := true
		blockOwnerDeletion := true
		job.OwnerReferences = append(job.OwnerReferences, metav1.OwnerReference{
			APIVersion:         aevrelv1.GroupVersion.String(),
			Kind:               "CheckpointMigration",
			Name:               migration.Name,
			UID:                migration.UID,
			Controller:         &controller,
			BlockOwnerDeletion: &blockOwnerDeletion,
		})
	}

	env := []corev1.EnvVar{
		{Name: "AEVREL_TARGET_READY", Value: "1"},
		{Name: "AEVREL_REPORT_SCHEMA_VERSION", Value: targetready.SchemaVersion},
		{Name: "AEVREL_REPORT_PATH", Value: targetReadyReportPath},
		{Name: "AEVREL_MIGRATION_UID", Value: migrationUID(migration)},
		{Name: "AEVREL_VERSION", Value: settings.Version},
		{Name: "AEVREL_TARGET_WORLD_SIZE", Value: strconv.Itoa(
			int(migration.Spec.Target.WorldSize),
		)},
		{Name: "AEVREL_VALIDATION_STEPS", Value: strconv.Itoa(
			int(settings.Steps),
		)},
		{Name: "AEVREL_CHECKPOINT_TEMPLATE", Value: migration.Spec.Target.RootTemplate},
	}
	if settings.StateTemplate != "" {
		env = append(
			env,
			corev1.EnvVar{
				Name:  "AEVREL_STATE_TEMPLATE",
				Value: settings.StateTemplate,
			},
			corev1.EnvVar{
				Name:  "AEVREL_STATE_PROVIDERS",
				Value: settings.StateProviders,
			},
			corev1.EnvVar{
				Name:  "AEVREL_REQUIRE_STATE",
				Value: boolEnv(settings.RequireState),
			},
		)
	}
	for index := range job.Spec.Template.Spec.Containers {
		container := &job.Spec.Template.Spec.Containers[index]
		container.Env = upsertEnv(container.Env, env...)
		container.TerminationMessagePath = targetReadyReportPath
		container.TerminationMessagePolicy = corev1.TerminationMessageReadFile
	}
	return &job, nil
}

func boolEnv(value bool) string {
	if value {
		return "1"
	}
	return "0"
}

func rawExtensionPayload(raw *runtime.RawExtension) ([]byte, error) {
	return rawExtensionPayloadFor(raw, "target.validation.jobTemplate")
}

func rawExtensionPayloadFor(
	raw *runtime.RawExtension,
	field string,
) ([]byte, error) {
	if raw == nil {
		return nil, fmt.Errorf("%s is empty", field)
	}
	if len(raw.Raw) > 0 {
		return raw.Raw, nil
	}
	if raw.Object != nil {
		payload, err := json.Marshal(raw.Object)
		if err != nil {
			return nil, fmt.Errorf("encode %s: %w", field, err)
		}
		return payload, nil
	}
	return nil, fmt.Errorf("%s is empty", field)
}

func targetValidationJobName(migration *aevrelv1.CheckpointMigration) string {
	if name := migration.Spec.Target.Validation.JobName; name != "" {
		return name
	}
	return deterministicJobName(migration.Name, "-target-ready")
}

func deterministicJobName(resourceName, suffix string) string {
	base := strings.TrimRight(resourceName, "-.")
	maxBase := targetReadyJobNameLimit - len(suffix)
	if len(base) > maxBase {
		sum := sha256.Sum256([]byte(resourceName))
		hash := hex.EncodeToString(sum[:4])
		prefixLength := maxBase - len(hash) - 1
		base = strings.TrimRight(base[:prefixLength], "-.") + "-" + hash
	}
	return base + suffix
}

func upsertEnv(existing []corev1.EnvVar, values ...corev1.EnvVar) []corev1.EnvVar {
	index := make(map[string]int, len(existing))
	for position, item := range existing {
		index[item.Name] = position
	}
	result := append([]corev1.EnvVar(nil), existing...)
	for _, item := range values {
		if position, ok := index[item.Name]; ok {
			result[position] = item
			continue
		}
		index[item.Name] = len(result)
		result = append(result, item)
	}
	return result
}

func int32Pointer(value int32) *int32 {
	return &value
}
