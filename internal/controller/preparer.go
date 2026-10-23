package controller

import (
	"context"
	"fmt"
	"sync"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/workqueue"
	"sigs.k8s.io/controller-runtime/pkg/client"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
	"github.com/bolubo/aevrel/internal/execution"
)

type PreparationRunner struct {
	client       client.Client
	orchestrator *Orchestrator
	resolver     *AgentResolver
	execution    *execution.Runner
	queue        workqueue.TypedRateLimitingInterface[types.NamespacedName]
	workers      int
}

func NewPreparationRunner(
	client client.Client,
	orchestrator *Orchestrator,
	executionRunner *execution.Runner,
	workers int,
) *PreparationRunner {
	if workers < 1 {
		workers = 2
	}
	return &PreparationRunner{
		client:       client,
		orchestrator: orchestrator,
		execution:    executionRunner,
		queue: workqueue.NewTypedRateLimitingQueue(
			workqueue.DefaultTypedControllerRateLimiter[types.NamespacedName](),
		),
		workers: workers,
	}
}

func (p *PreparationRunner) Enqueue(key types.NamespacedName) {
	p.queue.Add(key)
}

func (p *PreparationRunner) SetResolver(resolver *AgentResolver) {
	p.resolver = resolver
}

func (p *PreparationRunner) Start(ctx context.Context) error {
	var waitGroup sync.WaitGroup
	for worker := 0; worker < p.workers; worker++ {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			for {
				key, shutdown := p.queue.Get()
				if shutdown {
					return
				}
				err := p.process(ctx, key)
				p.queue.Done(key)
				if err != nil {
					p.queue.AddRateLimited(key)
				} else {
					p.queue.Forget(key)
				}
			}
		}()
	}
	<-ctx.Done()
	p.queue.ShutDown()
	waitGroup.Wait()
	return nil
}

func (p *PreparationRunner) NeedLeaderElection() bool {
	return true
}

func (p *PreparationRunner) process(
	ctx context.Context,
	key types.NamespacedName,
) error {
	var migration aevrelv1.CheckpointMigration
	if err := p.client.Get(ctx, key, &migration); err != nil {
		if client.IgnoreNotFound(err) == nil {
			return nil
		}
		return err
	}
	if migration.Status.ExecutionRef != "" {
		return nil
	}
	effective := &migration
	if p.resolver != nil {
		resolved, err := p.resolver.Resolve(ctx, &migration)
		if err != nil {
			return p.setFailed(ctx, &migration, err)
		}
		effective = resolved
	}
	if err := validateMigration(effective); err != nil {
		return p.setFailed(ctx, &migration, err)
	}
	if p.orchestrator == nil || p.execution == nil {
		return fmt.Errorf("preparation dependencies are not configured")
	}
	prepared, err := p.orchestrator.Prepare(ctx, effective)
	if err != nil {
		return p.setFailed(ctx, &migration, err)
	}
	run := buildExecutionRun(effective, prepared)
	p.execution.Ensure(run)
	if err := p.execution.Persist(ctx, run); err != nil {
		return p.setFailed(ctx, &migration, err)
	}
	migration.Status.Phase = aevrelv1.MigrationPhasePending
	migration.Status.PlanHash = prepared.PlanHash
	migration.Status.ExecutionRef = run.ExecutionRef()
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionTrue,
		Reason:             "PlanReady",
		Message:            "inventory and plan are ready",
		ObservedGeneration: migration.Generation,
	})
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "PlanReady",
		Status:             metav1.ConditionTrue,
		Reason:             "PlanBuilt",
		Message:            "plan artifact hash " + prepared.PlanHash,
		ObservedGeneration: migration.Generation,
	})
	return p.client.Status().Update(ctx, &migration)
}

func (p *PreparationRunner) setFailed(
	ctx context.Context,
	migration *aevrelv1.CheckpointMigration,
	err error,
) error {
	migration.Status.Phase = aevrelv1.MigrationPhaseFailed
	meta.SetStatusCondition(&migration.Status.Conditions, metav1.Condition{
		Type:               "Accepted",
		Status:             metav1.ConditionFalse,
		Reason:             "PreparationFailed",
		Message:            err.Error(),
		ObservedGeneration: migration.Generation,
	})
	return p.client.Status().Update(ctx, migration)
}

func buildExecutionRun(
	migration *aevrelv1.CheckpointMigration,
	prepared Prepared,
) *execution.Run {
	run := execution.NewRun(execution.RunConfig{
		ID:             migration.Namespace + "/" + migration.Name + ":" + prepared.PlanHash,
		PlanID:         prepared.PlanHash,
		MigrationUID:   migrationUID(migration),
		TargetWorld:    migration.Spec.Target.WorldSize,
		SpecRef:        prepared.SpecRef,
		PlanRef:        prepared.PlanRef,
		RootTemplate:   migration.Spec.Target.RootTemplate,
		Plan:           prepared.Plan,
		Endpoints:      targetEndpoints(migration),
		MaxAttempts:    int(migration.Spec.Policy.MaxAttemptsPerRank),
		MaxConcurrency: int(migration.Spec.Policy.MaxConcurrentTransfers),
		RequireReport:  true,
	})
	for rank := int32(0); rank < migration.Spec.Target.WorldSize; rank++ {
		run.RegisterRank(rank)
	}
	return run
}

func targetEndpoints(migration *aevrelv1.CheckpointMigration) map[int32]string {
	endpoints := make(map[int32]string, len(migration.Spec.Target.RankMap))
	for _, entry := range migration.Spec.Target.RankMap {
		endpoints[entry.Rank] = entry.Endpoint
	}
	return endpoints
}
