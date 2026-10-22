package v1

import "k8s.io/apimachinery/pkg/runtime"

func init() {
	SchemeBuilder.Register(
		&CheckpointRecord{},
		&CheckpointRecordList{},
		&RestoreDrill{},
		&RestoreDrillList{},
		&RescueDecision{},
		&RescueDecisionList{},
		&MobilityTransaction{},
		&MobilityTransactionList{},
	)
}

var _ runtime.Object = &CheckpointRecord{}
var _ runtime.Object = &RestoreDrill{}
var _ runtime.Object = &RescueDecision{}
var _ runtime.Object = &MobilityTransaction{}
