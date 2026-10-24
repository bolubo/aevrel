# Triage

## Response expectations

Triage is best effort. There is no guaranteed response SLA. Security reports
follow [SECURITY.md](SECURITY.md) and must not be filed publicly.

## Labels

| Label | Meaning |
|---|---|
| `kind/bug` | Reproducible correctness or runtime failure |
| `kind/feature` | New user-facing capability |
| `kind/docs` | Documentation or developer experience |
| `kind/security` | Security-sensitive; use private reporting |
| `kind/test` | Test coverage or test infrastructure |
| `kind/refactor` | Non-behavioral code or structure improvement |
| `question` | Usage or operation question |
| `area/control-plane` | Readiness, rescue, policy, proofs, contracts |
| `area/data-plane` | DCP/runtime adapters, transport, commit |
| `area/kubernetes` | Controller, CRDs, agents, Helm |
| `area/kueue` | Kueue adapter and scheduler integration |
| `area/lrs` | Live rank split |
| `area/ui` | Web UI or CLI experience |
| `needs-repro` | Reporter must provide a minimal reproduction |
| `needs-design` | Contract or architecture decision is unresolved |
| `design/rfc` | Proposal needs a documented design decision |
| `dependencies` | Dependency or toolchain update |
| `blocked` | Waiting on an external dependency or owner decision |
| `good first issue` | Suitable for a first contribution |
| `help wanted` | Maintainer would welcome a contributor |
| `breaking-change` | Changes a public contract or upgrade path |

## Process

1. Check whether the report is a security issue; route it privately if so.
2. Ask for the smallest reproduction, version/commit, and sanitized evidence.
3. Label the affected area and add `needs-repro` or `needs-design` when needed.
4. Close only after a maintainer decision, duplicate resolution, or explicit
   reporter confirmation.
5. Keep claimable starter issues unassigned until a contributor comments with a
   plan or a maintainer starts work. Assign only the person currently working
   on it; do not use assignment as a placeholder for ownership.

There is no automatic stale closure. Issues that are not actionable should be
closed manually with a reason and can be reopened when new evidence appears.
