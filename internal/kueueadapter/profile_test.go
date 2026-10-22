package kueueadapter

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestCheckStrictProfile(t *testing.T) {
	tests := []struct {
		name       string
		config     *unstructured.Unstructured
		queue      *unstructured.Unstructured
		wantQueues []string
		wantErr    string
	}{
		{
			name:       "valid",
			config:     testPreemptionConfig(1),
			queue:      testClusterQueue("Never", "Never", "Never"),
			wantQueues: []string{"guard-cq"},
		},
		{
			name:       "missing strict rule",
			config:     testPreemptionConfig(0),
			queue:      testClusterQueue("Never", "Never", "Never"),
			wantQueues: []string{"guard-cq"},
			wantErr:    "no strict numeric label rule",
		},
		{
			name:       "classical preemption enabled",
			config:     testPreemptionConfig(1),
			queue:      testClusterQueue("LowerPriority", "Never", "Never"),
			wantQueues: []string{"guard-cq"},
			wantErr:    "withinClusterQueue must be Never",
		},
		{
			name:    "no clusterqueue references config",
			config:  testPreemptionConfig(1),
			queue:   nil,
			wantErr: "no ClusterQueue references",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			objects := []client.Object{test.config}
			if test.queue != nil {
				objects = append(objects, test.queue)
			}
			reader := fake.NewClientBuilder().
				WithScheme(testProfileScheme(t)).
				WithObjects(objects...).
				Build()
			report, err := CheckStrictProfile(
				context.Background(),
				reader,
				"checkpoint-guard",
			)
			if test.wantErr == "" {
				if err != nil {
					t.Fatalf("CheckStrictProfile() error = %v", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("CheckStrictProfile() error = %v, want %q", err, test.wantErr)
			}
			if strings.Join(report.ClusterQueues, ",") != strings.Join(test.wantQueues, ",") {
				t.Fatalf("queues = %v, want %v", report.ClusterQueues, test.wantQueues)
			}
		})
	}
}

func testProfileScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := testScheme(t)
	AddToScheme(scheme, ClusterQueueGVK)
	AddToScheme(scheme, PreemptionConfigGVK)
	return scheme
}

func testPreemptionConfig(fallback int64) *unstructured.Unstructured {
	return &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": "kueue.x-k8s.io/v1alpha1",
			"kind":       "PreemptionConfig",
			"metadata": map[string]any{
				"name": "checkpoint-guard",
			},
			"spec": map[string]any{
				"rules": []any{
					map[string]any{
						"name": "exclude-protected-workloads",
						"candidateSelectors": []any{
							map[string]any{
								"scope": "AnyClusterQueue",
								"numericLabels": []any{
									map[string]any{
										"key":           LabelPreemptionProtected,
										"maxValue":      int64(0),
										"fallbackValue": fallback,
									},
								},
							},
						},
					},
				},
			},
		},
	}
}

func testClusterQueue(
	within string,
	reclaim string,
	borrow string,
) *unstructured.Unstructured {
	return &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": "kueue.x-k8s.io/v1beta2",
			"kind":       "ClusterQueue",
			"metadata": map[string]any{
				"name": "guard-cq",
				"annotations": map[string]any{
					"kueue.x-k8s.io/preemption-config-name": "checkpoint-guard",
				},
			},
			"spec": map[string]any{
				"preemption": map[string]any{
					"withinClusterQueue":  within,
					"reclaimWithinCohort": reclaim,
					"borrowWithinCohort": map[string]any{
						"policy": borrow,
					},
				},
			},
		},
	}
}
