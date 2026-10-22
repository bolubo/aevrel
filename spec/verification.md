# shardferry MVP Verification Plan v1

## Principles

- The implementation must not be its own oracle.
- Cross-machine transfer is mandatory; local-only tests are insufficient.
- Mutation tests must prove the checker catches deliberate bugs.
- An independent user must reproduce the flow from the README.

## Verification levels

| Level | What | Pass |
|---|---|---|
| L0 Unit | manifest, mesh, chunk intersection, plan determinism, checksum, CLI errors | all green |
| L1 Property | random shapes, meshes, placements, uneven shards, partial overlap | every target byte covered once; plan bytes <= 1.2x minimum |
| L2 Oracle | brute-force gather-to-global + re-shard | bitwise equal to shardferry output |
| L3 Multi-process | real DCP small model, gloo, 4->1/1->4/4->2/4->3/2x2->3x1 | target loads and outputs bitwise match |
| L4 Cross-machine | two machines, separate disks, no shared FS, direct HTTP | only necessary chunks move; byte counts match plan |
| L5 Failure | kill source/target, network cut, missing shard, checksum, disk full | resume/idempotent, source untouched, clear error |
| L6 Scale | 4->3/8->5/16->7, 1GB/10GB synthetic | no full gather; bytes/time/memory reported |
| L7 Compatibility | two PyTorch versions, DCP/FSDP/DTensor, optimizer state, bf16/fp32 | at least two versions pass |
| L8 Independent user | classmate follows README from scratch | success without author help |
| L9 Mutation | drop/duplicate/shift/wrong placement/checksum skip/hidden full gather | corresponding test must fail |

## Cross-machine test

Two machines A and B, separate local disks, no shared directory:

```bash
# A
shardferry serve --manifest source.yaml --listen 0.0.0.0:9000

# B
shardferry apply --plan plan.json --source http://A:9000
```

Measure bytes served/received and compare with the plan. Verify on B; A
independently generates the global tensor/checksum for cross-checking.

## Acceptance gates

1. L0-L3 all green.
2. L4 passes and byte counts match the plan.
3. L5 passes with source untouched and resumable apply.
4. L6 proves bytes <= 1.2x theoretical minimum and no full gather.
5. L7 passes for at least two PyTorch versions, FSDP/DTensor, optimizer state.
6. L8 passes.
7. L9 mutation tests all fail as expected.
8. Real DCP 4->3 load/output matches.

Any failed gate means the MVP is not complete.
