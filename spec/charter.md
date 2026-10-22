# shardferry Project Charter v1.0

## Definition

`shardferry` moves local sharded checkpoints from a source DeviceMesh/cluster to a
target DeviceMesh/cluster, resharding them for the target world size and
placement without a global remote checkpoint.

## User

AI lab / cloud training-platform engineers running distributed PyTorch training
on Kubernetes, with checkpoint shards written to local disks.

## User story

A 4-rank training job has local shards. It must resume on a 3-rank cluster with a
different mesh. The user runs `shardferry inspect -> plan -> apply -> verify` and
does not upload a consolidated checkpoint to S3 first.

## MVP scope

- CLI: `inspect`, `plan`, `apply`, `verify`, `serve`.
- Input: MigrationSpec YAML.
- Support: toy state_dict + one real PyTorch DCP checkpoint.
- 1D/2D DeviceMesh, different world sizes, Shard/Replicate/Partial, partial overlap.
- Transports: `file://`, direct `http://` source agent, optional `s3://` relay.
- CPU/gloo; GPU optional.
- Outputs: Plan JSON, Report JSON.

## MVP acceptance

1. 4->3 partial-overlap migration is bitwise correct.
2. Placement and 1D<->2D mesh changes are bitwise correct.
3. 4->1 and 1->4 world-size changes are bitwise correct.
4. A real DCP checkpoint migrates 4->3 and loads with identical outputs.
5. Plan bytes are at most 1.2x the theoretical minimum; no full gather.
6. Interrupted apply resumes; no duplicate/corrupt target shards.
7. Missing shard, checksum mismatch, invalid manifest, and write failure fail
   clearly and leave the source untouched.
8. Report contains plan id, meshes, tensors, chunks, bytes, time, retries, verification.
9. CPU 4->3 toy demo runs in under 5 minutes and is CI-reproducible.
10. README, schema docs, supported topologies, and limitations are documented.

## Out of scope

K8s operator/CRD, Kubeflow/Kueue integration, multi-cluster discovery, CRIU or
cuda-checkpoint, live migration, encryption/multi-tenancy/RBAC beyond basic
credentials, NFS/PVC/RDMA, arbitrary checkpoint formats, UI, upstream changes,
and performance work beyond minimal necessary transfer.

## Stack

Python 3.11+ for the MVP. JSON/YAML contracts are language-neutral. A later Go
operator can use the same contracts without rewriting the core.

## Change control

Anything outside these acceptance criteria goes to the backlog. Scope changes
require explicit owner approval and a charter revision.
