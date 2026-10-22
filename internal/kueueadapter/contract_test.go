package kueueadapter

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestParseObservation(t *testing.T) {
	tests := []struct {
		name        string
		annotations map[string]string
		labels      map[string]string
		want        Observation
		wantErr     string
	}{
		{
			name: "missing",
			want: Observation{},
		},
		{
			name: "annotations",
			annotations: map[string]string{
				AnnotationProgressPercent:    "85",
				AnnotationCheckpointComplete: "true",
				AnnotationCheckpointVersion:  "step-8500",
				AnnotationCheckpointAge:      "12",
			},
			want: Observation{
				Known:                   true,
				TrainingProgressPercent: 85,
				CheckpointComplete:      true,
				CheckpointVersion:       "step-8500",
				CheckpointAgeSeconds:    int64Ptr(12),
			},
		},
		{
			name: "labels fallback",
			labels: map[string]string{
				AnnotationProgressPercent:    "40",
				AnnotationCheckpointComplete: "0",
			},
			want: Observation{
				Known:                   true,
				TrainingProgressPercent: 40,
				CheckpointComplete:      false,
			},
		},
		{
			name: "annotation wins",
			annotations: map[string]string{
				AnnotationProgressPercent: "20",
			},
			labels: map[string]string{
				AnnotationProgressPercent: "80",
			},
			want: Observation{
				Known:                   true,
				TrainingProgressPercent: 20,
			},
		},
		{
			name: "invalid progress",
			annotations: map[string]string{
				AnnotationProgressPercent: "101",
			},
			want: Observation{
				Invalid: true,
			},
			wantErr: "between 0 and 100",
		},
		{
			name: "invalid checkpoint complete",
			annotations: map[string]string{
				AnnotationCheckpointComplete: "maybe",
			},
			want: Observation{
				Invalid: true,
			},
			wantErr: "must be one of",
		},
		{
			name: "invalid age",
			annotations: map[string]string{
				AnnotationCheckpointAge: "-1",
			},
			want: Observation{
				Invalid: true,
			},
			wantErr: "non-negative",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := ParseObservation(test.annotations, test.labels)
			if test.wantErr == "" {
				if err != nil {
					t.Fatalf("ParseObservation() error = %v", err)
				}
			} else if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("ParseObservation() error = %v, want %q", err, test.wantErr)
			}
			if got.Known != test.want.Known ||
				got.Invalid != test.want.Invalid ||
				got.TrainingProgressPercent != test.want.TrainingProgressPercent ||
				got.CheckpointComplete != test.want.CheckpointComplete ||
				got.CheckpointVersion != test.want.CheckpointVersion {
				t.Fatalf("ParseObservation() = %#v, want %#v", got, test.want)
			}
			if !equalInt64Ptr(got.CheckpointAgeSeconds, test.want.CheckpointAgeSeconds) {
				t.Fatalf(
					"checkpoint age = %v, want %v",
					got.CheckpointAgeSeconds,
					test.want.CheckpointAgeSeconds,
				)
			}
		})
	}
}

func TestIsProtectionEnabled(t *testing.T) {
	tests := []struct {
		name   string
		object metav1.Object
		want   bool
	}{
		{
			name: "annotation",
			object: &metav1.ObjectMeta{
				Annotations: map[string]string{
					AnnotationProtection: "enabled",
				},
			},
			want: true,
		},
		{
			name: "label true",
			object: &metav1.ObjectMeta{
				Labels: map[string]string{
					AnnotationProtection: "true",
				},
			},
			want: true,
		},
		{
			name: "disabled",
			object: &metav1.ObjectMeta{
				Annotations: map[string]string{
					AnnotationProtection: "disabled",
				},
			},
		},
		{
			name:   "missing",
			object: &metav1.ObjectMeta{},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := IsProtectionEnabled(test.object); got != test.want {
				t.Fatalf("IsProtectionEnabled() = %t, want %t", got, test.want)
			}
		})
	}
}

func int64Ptr(value int64) *int64 {
	return &value
}

func equalInt64Ptr(left, right *int64) bool {
	if left == nil || right == nil {
		return left == right
	}
	return *left == *right
}
