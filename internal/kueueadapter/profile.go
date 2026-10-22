package kueueadapter

import (
	"context"
	"fmt"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	ClusterQueueGVK = schema.GroupVersionKind{
		Group:   KueueGroup,
		Version: "v1beta2",
		Kind:    "ClusterQueue",
	}
	PreemptionConfigGVK = schema.GroupVersionKind{
		Group:   KueueGroup,
		Version: "v1alpha1",
		Kind:    "PreemptionConfig",
	}
)

// ProfileReport is the result of checking the deployment-side half of the
// binary guard. It is intentionally separate from the per-workload policy.
type ProfileReport struct {
	ConfigName    string
	ClusterQueues []string
}

// CheckStrictProfile verifies the parts of configurable-only enforcement that
// can be observed from Kubernetes objects. Fair Sharing is a global Kueue
// setting and must also be disabled by the operator; this function cannot infer
// it from a ClusterQueue.
func CheckStrictProfile(
	ctx context.Context,
	reader client.Reader,
	configName string,
) (ProfileReport, error) {
	report := ProfileReport{ConfigName: configName}
	if reader == nil {
		return report, fmt.Errorf("profile reader is nil")
	}
	if configName == "" {
		return report, fmt.Errorf("preemption-config-name is required in strict mode")
	}

	config := &unstructured.Unstructured{}
	config.SetGroupVersionKind(PreemptionConfigGVK)
	if err := reader.Get(
		ctx,
		client.ObjectKey{Name: configName},
		config,
	); err != nil {
		return report, fmt.Errorf("read PreemptionConfig %q: %w", configName, err)
	}
	var problems []string
	if !hasStrictCandidateRule(config) {
		problems = append(
			problems,
			fmt.Sprintf(
				"PreemptionConfig %q has no strict numeric label rule for %s",
				configName,
				LabelPreemptionProtected,
			),
		)
	}

	queues := &unstructured.UnstructuredList{}
	queues.SetGroupVersionKind(
		ClusterQueueGVK.GroupVersion().WithKind("ClusterQueueList"),
	)
	if err := reader.List(ctx, queues); err != nil {
		return report, fmt.Errorf("list ClusterQueues: %w", err)
	}
	for i := range queues.Items {
		queue := &queues.Items[i]
		if queue.GetAnnotations()["kueue.x-k8s.io/preemption-config-name"] != configName {
			continue
		}
		report.ClusterQueues = append(report.ClusterQueues, queue.GetName())
		if reason := classicalPreemptionReason(queue); reason != "" {
			problems = append(
				problems,
				fmt.Sprintf("ClusterQueue %q is not configurable-only: %s", queue.GetName(), reason),
			)
		}
	}
	if len(report.ClusterQueues) == 0 {
		problems = append(
			problems,
			fmt.Sprintf("no ClusterQueue references PreemptionConfig %q", configName),
		)
	}
	sort.Strings(report.ClusterQueues)
	if len(problems) > 0 {
		return report, fmt.Errorf(
			"strict profile is not ready: %s",
			strings.Join(problems, "; "),
		)
	}
	return report, nil
}

func hasStrictCandidateRule(config *unstructured.Unstructured) bool {
	rules, found, _ := unstructured.NestedSlice(config.Object, "spec", "rules")
	if !found {
		return false
	}
	for _, rawRule := range rules {
		rule, ok := rawRule.(map[string]any)
		if !ok {
			continue
		}
		selectors, found, _ := unstructured.NestedSlice(
			rule,
			"candidateSelectors",
		)
		if !found {
			continue
		}
		for _, rawSelector := range selectors {
			selector, ok := rawSelector.(map[string]any)
			if !ok {
				continue
			}
			constraints, found, _ := unstructured.NestedSlice(
				selector,
				"numericLabels",
			)
			if !found {
				continue
			}
			for _, rawConstraint := range constraints {
				constraint, ok := rawConstraint.(map[string]any)
				if !ok {
					continue
				}
				key, _, _ := unstructured.NestedString(constraint, "key")
				maxValue, hasMax, _ := unstructured.NestedInt64(
					constraint,
					"maxValue",
				)
				fallback, hasFallback, _ := unstructured.NestedInt64(
					constraint,
					"fallbackValue",
				)
				_, hasComparison, _ := unstructured.NestedString(
					constraint,
					"comparison",
				)
				if key == LabelPreemptionProtected &&
					hasMax &&
					maxValue == 0 &&
					hasFallback &&
					fallback == 1 &&
					!hasComparison {
					return true
				}
			}
		}
	}
	return false
}

func classicalPreemptionReason(queue *unstructured.Unstructured) string {
	within, _, _ := unstructured.NestedString(
		queue.Object,
		"spec",
		"preemption",
		"withinClusterQueue",
	)
	if within != "" && within != "Never" {
		return "withinClusterQueue must be Never"
	}
	reclaim, _, _ := unstructured.NestedString(
		queue.Object,
		"spec",
		"preemption",
		"reclaimWithinCohort",
	)
	if reclaim != "" && reclaim != "Never" {
		return "reclaimWithinCohort must be Never"
	}
	borrow, _, _ := unstructured.NestedString(
		queue.Object,
		"spec",
		"preemption",
		"borrowWithinCohort",
		"policy",
	)
	if borrow != "" && borrow != "Never" {
		return "borrowWithinCohort.policy must be Never"
	}
	return ""
}
