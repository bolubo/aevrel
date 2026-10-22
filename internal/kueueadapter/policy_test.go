package kueueadapter

import "testing"

func TestPolicyDecide(t *testing.T) {
	tests := []struct {
		name        string
		policy      Policy
		observation Observation
		want        Decision
	}{
		{
			name:        "invalid state protected",
			policy:      DefaultPolicy(),
			observation: Observation{Invalid: true},
			want:        protected(ReasonInvalidStateProtected),
		},
		{
			name: "invalid state fail open",
			policy: Policy{
				ProtectProgressGTE:        0.8,
				RequireCheckpointComplete: true,
				ProtectUnknown:            false,
			},
			observation: Observation{Invalid: true},
			want:        eligible(ReasonInvalidStateEligible),
		},
		{
			name:        "unknown state protected",
			policy:      DefaultPolicy(),
			observation: Observation{},
			want:        protected(ReasonUnknownStateProtected),
		},
		{
			name: "unknown state fail open",
			policy: Policy{
				ProtectProgressGTE:        0.8,
				RequireCheckpointComplete: true,
				ProtectUnknown:            false,
			},
			observation: Observation{},
			want:        eligible(ReasonUnknownStateEligible),
		},
		{
			name:   "checkpoint incomplete",
			policy: DefaultPolicy(),
			observation: Observation{
				Known:                   true,
				TrainingProgressPercent: 70,
				CheckpointComplete:      false,
			},
			want: protected(ReasonCheckpointIncomplete),
		},
		{
			name: "checkpoint completeness optional",
			policy: Policy{
				ProtectProgressGTE:        0.8,
				RequireCheckpointComplete: false,
				ProtectUnknown:            true,
			},
			observation: Observation{
				Known:                   true,
				TrainingProgressPercent: 70,
				CheckpointComplete:      false,
			},
			want: eligible(ReasonEligible),
		},
		{
			name:   "progress threshold exact",
			policy: DefaultPolicy(),
			observation: Observation{
				Known:                   true,
				TrainingProgressPercent: 80,
				CheckpointComplete:      true,
			},
			want: protected(ReasonProgressThreshold),
		},
		{
			name:   "progress below threshold",
			policy: DefaultPolicy(),
			observation: Observation{
				Known:                   true,
				TrainingProgressPercent: 79,
				CheckpointComplete:      true,
			},
			want: eligible(ReasonEligible),
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := test.policy.Decide(test.observation)
			if got != test.want {
				t.Fatalf("Decide() = %#v, want %#v", got, test.want)
			}
			if got.Protected == got.Eligible {
				t.Fatalf("decision must be exactly one of protected or eligible: %#v", got)
			}
		})
	}
}

func TestPolicyValidate(t *testing.T) {
	tests := []struct {
		name    string
		policy  Policy
		wantErr bool
	}{
		{name: "default", policy: DefaultPolicy()},
		{
			name: "lower bound",
			policy: Policy{
				ProtectProgressGTE: 0,
			},
		},
		{
			name: "upper bound",
			policy: Policy{
				ProtectProgressGTE: 1,
			},
		},
		{
			name: "negative",
			policy: Policy{
				ProtectProgressGTE: -0.1,
			},
			wantErr: true,
		},
		{
			name: "above one",
			policy: Policy{
				ProtectProgressGTE: 1.1,
			},
			wantErr: true,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := test.policy.Validate()
			if (err != nil) != test.wantErr {
				t.Fatalf("Validate() error = %v, wantErr %t", err, test.wantErr)
			}
		})
	}
}
