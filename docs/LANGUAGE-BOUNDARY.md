# ADR: Native Language Boundary

- **Status:** Proposed — awaiting owner approval
- **Target path in repo:** `docs/LANGUAGE-BOUNDARY.md`
- **Scope:** Language assignment for Aevrel's own code. No business-logic changes (0.4 work is out of scope).
- **Compatibility:** Not a concern. Aevrel is an early-stage project; deprecated code paths may be deleted outright rather than kept for compatibility.

## Context

Aevrel is a scheduler-neutral control plane for training state, written across
multiple languages. The intended split was always:

- **Go** — Kubernetes control-plane authority: controllers, authoritative
  objects (`CheckpointRecord`, `CheckpointReadiness`, `RestoreDrill`,
  `RescueDecision`, `CheckpointMigration`), `aevrelctl`, gateway, Kueue adapter.
- **Python** — training-runtime and data-plane adapters: read-only checkpoint
  inspection, evidence collection, agent inventory, CLI orchestration.
- **Rust** — native data-plane hot path and benchmark helpers.

In practice the boundary drifted during AI-assisted development. Concretely:

1. **Rust is almost absent from the hot path it was meant to own.** The Rust
   crate `native/aevrel-fast` contains a single source file implementing content
   -defined chunking (CDC) plus content-addressable store ingest. Meanwhile the
   byte-range, reduction and proof kernels are all Python:
   `planning/tensor_io.py` (`byte_runs`, `coalesce_runs`, `reduce_bytes`),
   `executor.py` (`verify_source_checksums` and byte-range copies), and
   `verification/proof.py` (Merkle root and HMAC). The `lrs/` subsystem
   (~6k lines) is Python too, although measurement later showed most of it is
   numpy-bound or runs once per migration rather than per byte — see
   "Not claimed, deliberately" under the Rust quadrant.
2. **The CDC algorithm is implemented twice**, once in Rust
   (`native/aevrel-fast/src/main.rs`) and once in Python
   (`src/aevrel/cdp.py`, including its own gear-hash table duplicating the Rust
   `gear()` function). `src/aevrel/native.py` silently falls back to the Python
   implementation when the Rust binary is unavailable, so the source of truth is
   ambiguous and, in practice, Python is the active path.
3. **Real data-plane logic lives as loose scripts** under `scripts/` (LRS
   experiments, GPU save/load operations) instead of inside proper adapters,
   obscuring which language owns what.
4. **Two CLIs share command verbs.** `aevrel` (Python, local data plane) and
   `aevrelctl` (Go, cluster control plane) both expose `migrate`, `readiness`,
   and `plan`. The domains are disjoint, but the command surface does not reveal
   the layering.

This ADR fixes the language assignment so that ownership is unambiguous.

## Decision

**Rust is the language for Aevrel's own native hot path.** C and C++ are not
used to implement Aevrel's own hot-path code; they appear only as thin FFI
shims when Aevrel must call an external C/C++ library.

### Rationale

The stated goals are **speed** and **safety**. Speed alone does not decide the
question: Rust, C, and C++ all reach effectively identical peak performance for
the workloads in question (byte movement, SHA-256, Merkle hashing, numerical
kernels). The decision is therefore made on safety and on three consequences
specific to this project.

| Axis | Rust | C++ | C |
|---|---|---|---|
| Speed | Ties with C/C++ (release build, zero-cost abstractions) | Ties with C | Baseline |
| Memory and concurrency safety | Guaranteed at compile time (borrow checker, no data races) | Depends on discipline; unsafe by default | Essentially none: buffer overflows and use-after-free are common |
| Integration with Go + Python | Works via C ABI (cgo), PyO3, or the existing subprocess pattern | Works via C ABI; no existing investment | Works via C ABI; everything hand-written |
| Suitability for AI-assisted development | Compiler rejects memory and concurrency bugs before they land | Compiler catches little; AI-written C++ accumulates UB and leaks | Worst case |
| Fit with existing codebase | `native/aevrel-fast` already exists, with `Cargo.toml`, a release build, and `native.py` calling it | Would discard existing work and re-implement hashing, JSON, and threading by hand | Same |

Three points decide it:

1. **Safety is a compile-time property only in Rust.** Aevrel's data plane
   processes checkpoint files that are not fully trusted and performs chunking,
   hashing, and file ingest. Re-introducing manual memory management (C) or
   discipline-dependent safety (C++) reopens exactly the risk class that choosing
   a memory-safe control-plane language (Go) was meant to avoid.
2. **The borrow checker is a safety net for AI-assisted development.** The
   boundary drift documented above is largely a product of mixed, AI-assisted
   writing. Rust is the only candidate whose compiler rejects whole classes of
   those defects before they land.
3. **The project already chose the safe systems language elsewhere.** Go for the
   control plane and Rust for the native path is a consistent posture; C/C++
   would be a regression.

C and C++ are justified only at the boundary to third-party code, never as the
implementation language for Aevrel's own kernels.

## Boundaries

### Go — control-plane authority

**Owns:** Kubernetes authoritative objects and their reconciliation; controllers;
`aevrelctl` (cluster-side commands); gateway; Kueue adapter; cluster-facing
orchestration.

**Does not own:** checkpoint inspection, byte movement, hashing, or numerical
kernels. Go orchestrates and records; it does not implement the data-plane
computation.

### Python — data-plane adapters and glue

**Owns:** read-only checkpoint inspection (DCP metadata, rank-local shards,
state bundles); evidence collection; agent read-only inventory and target
validation; local CLI orchestration (`aevrel`); file and storage I/O; schema
validation; test and acceptance harnesses.

**Does not own:** compute-bound hot kernels. Python keeps orchestration and I/O;
it delegates hot computation to Rust rather than implementing it.

### Rust — native hot path

**Owns (target state):**

- Content-defined chunking, SHA-256, content-addressable store ingest, and
  manifest generation (already present; must become the single source of truth).
- Byte-range kernels: `byte_runs`, `coalesce_runs`, `split_runs`,
  `paired_run_batches`, `reduce_bytes`, plus the `lrs/` layout algebra's
  `byte_ranges`, which used to carry a second copy of the stride walk.
- Checksum and proof kernels: SHA-256 over bytes and files, the Merkle root.

**Not claimed, deliberately.** The rest of `lrs/` stays Python, and the reason
is measured rather than assumed:

- Its arithmetic already runs inside numpy, so per-element work is native
  today. `LRSSimulator` costs 315 µs per step on the toy configuration (pure
  Python overhead) but 440 ms per step at 262144 parameters, where the cost is
  linear in data and a Rust port would have to beat numpy's vector loops.
- Planning work (`plan_transfer`, `iter_shards`, `ModelVPS`) happens once per
  migration, not once per byte moved: 0.03 ms for the 4→3 layout the product
  actually uses, 22 ms at 256 shards.
- `sim`, `tp_pp`, and their proofs are the independent simulators the LRS
  protocols are validated against. Porting an oracle into the language it is
  used to validate destroys the independence that makes it worth having.

**Does not own:** orchestration, schema validation, or user-facing CLI behavior.
Rust exposes kernels; Python and Go retain the surrounding logic.

### C / C++ — external-library FFI shims only

**Permitted:** a thin FFI boundary when Aevrel must call a third-party C/C++
library — for example NCCL, PyTorch (CUDA), or a GPU codec.

**Forbidden:**

- Implementing Aevrel's own hot-path logic in C or C++.
- Business logic inside the shim. A shim translates types and calls; it contains
  no decisions about readiness, migration, or evidence.
- New `.c`, `.cpp`, `.cc`, `.h`, or `.hpp` files outside an explicitly
  designated `ffi` directory (for example `native/ffi/`).

## Ownership table

| Owner | Module / path | Current language | Action |
|---|---|---|---|
| Rust | `native/aevrel-fast` (CDC + CAS + kernels) | Rust | Single source of truth |
| Rust | `planning/tensor_io.py`: `byte_runs`, `coalesce_runs`, `split_runs`, `paired_run_batches`, `reduce_bytes` | Python wrappers over Rust kernels | Done (R2) |
| Rust | `verification/proof.py`: Merkle root | Python wrapper over a Rust kernel | Done (R2) |
| Rust | `lrs/layout.py`: `byte_ranges` | Python wrapper over Rust kernels | Done (R3) |
| Python | `verification/proof.py`: SHA-256 of one buffer, constant-time compare | Python | Keep — OpenSSL matches the native hash (0.99x–1.01x) and is 4% faster on a 4 MiB leaf |
| Python | `executor.py`: `verify_source_checksums`, byte-range copy | Python | Keep — I/O orchestration across storage backends, not a kernel |
| Python | `lrs/`: `sim`, `tp_pp`, `model_vps`, `transport`, `state_closure`, `planning` | Python | Keep — numpy-bound, once-per-migration, or the validation oracle |
| Python | DCP read-only inspection, readiness evidence, agent inventory, CLI orchestration, I/O | Python | Keep |
| Go | Controllers, `aevrelctl`, gateway, Kueue adapter | Go | Keep |
| C/C++ (FFI only) | NCCL / PyTorch-CUDA / GPU codec bindings | — | Boundary only |

## Enforcement

1. `scripts/check_repo.py` rejects new C/C++ sources outside an explicitly
   designated `ffi` directory.
2. CI builds and tests Rust: `cargo build --release`, `cargo test`,
   `cargo clippy -- -D warnings`.
3. When the Rust binary is unavailable, the dependent command fails loudly with a
   non-zero exit and an explicit "build the Rust binary first" error. No silent
   fallback to a Python implementation.
4. Every migrated kernel carries a golden-equivalence test proving the Rust
   output matches the removed Python implementation byte-for-byte (or within the
   project's established numerical tolerance for floating-point kernels, as in
   the LRS `dW ~ 1.4e-17` precedent).

## Anti-patterns explicitly forbidden

- Re-implementing a kernel that already exists in Rust, inside Python.
- Silently falling back to a Python implementation when Rust is unavailable.
- Writing business logic (readiness, migration, evidence decisions) inside an
  FFI shim.
- Leaving real data-plane logic in `scripts/` instead of a proper adapter.
- Merging to `main` without the golden-equivalence test for the migrated kernel.

## Consequences

- `src/aevrel/cdp.py` loses its duplicated chunking algorithm and becomes a thin
  caller of the Rust binary.
- Python modules keep orchestration and I/O but shed their hot loops; call sites
  change, and Python-side tests that exercised Python hot loops are replaced by
  golden-equivalence tests against Rust.
- The build gains a required Rust step; CI must build `native/aevrel-fast`
  before acceptance tests run.
- Migration is phased and each phase is independently verifiable; the phased
execution checklist and per-phase acceptance criteria are tracked in the
internal planning workspace.

## References

- `docs/ARCHITECTURE.md` — layer and invariant definitions.
- Internal planning workspace — phased execution checklist, phase ordering,
  and per-phase acceptance criteria.
