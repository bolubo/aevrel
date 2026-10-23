package main

import (
	"fmt"
	"io"
	"os"

	aevrelv1 "github.com/bolubo/aevrel/api/v1"
)

func isTerminalWriter(out io.Writer) bool {
	file, ok := out.(*os.File)
	if !ok {
		return false
	}
	info, err := file.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}

func progressBar(ratio float64, tty bool, width int) string {
	if ratio < 0 {
		ratio = 0
	}
	if ratio > 1 {
		ratio = 1
	}
	filled := int(ratio*float64(width) + 0.5)
	if filled < 0 {
		filled = 0
	}
	if filled > width {
		filled = width
	}
	fill := "#"
	empty := "-"
	if tty {
		fill = "█"
		empty = "░"
	}
	result := ""
	for index := 0; index < width; index++ {
		if index < filled {
			result += fill
		} else {
			result += empty
		}
	}
	return result
}

func progressBucket(migration *aevrelv1.CheckpointMigration) int {
	total, done := progressCounts(migration)
	if total <= 0 {
		return 0
	}
	return int(float64(done) / float64(total) * 20)
}

func progressCounts(migration *aevrelv1.CheckpointMigration) (int, int) {
	total := int(migration.Spec.Target.WorldSize)
	if total < 1 {
		total = 1
	}
	if live := migration.Status.Live; live != nil {
		settled := len(live.Coverage) - len(live.InFlight)
		if settled < 0 {
			settled = 0
		}
		return total, settled
	}
	return total, int(migration.Status.Counts.Committed)
}

func migrationProgressLine(migration *aevrelv1.CheckpointMigration, tty bool) string {
	total, done := progressCounts(migration)
	ratio := 0.0
	if total > 0 {
		ratio = float64(done) / float64(total)
	}
	if live := migration.Status.Live; live != nil {
		inFlight := len(live.InFlight)
		coverage := len(live.Coverage)
		maxInFlight := int32(0)
		if migration.Spec.Live != nil {
			maxInFlight = migration.Spec.Live.MaxInFlight
		}
		requiredQuorum := migration.Spec.Target.WorldSize/2 + 1
		quorum := live.Quorum
		if quorum <= 0 {
			quorum = requiredQuorum
		}
		return fmt.Sprintf(
			"live [%s] %3d%% settled=%d/%d coverage=%d/%d inFlight=%d/%d quorum=%d/%d stateComplete=%t phase=%s",
			progressBar(ratio, tty, 20),
			int(ratio*100+0.5),
			done,
			total,
			coverage,
			total,
			inFlight,
			maxInFlight,
			quorum,
			requiredQuorum,
			live.StateComplete,
			emptyDash(live.CutoverPhase),
		)
	}
	return fmt.Sprintf(
		"static [%s] %3d%% committed=%d/%d staged=%d prepared=%d failed=%d phase=%s",
		progressBar(ratio, tty, 20),
		int(ratio*100+0.5),
		done,
		total,
		migration.Status.Counts.Staged,
		migration.Status.Counts.Prepared,
		migration.Status.Counts.Failed,
		emptyDash(migration.Status.Phase),
	)
}
