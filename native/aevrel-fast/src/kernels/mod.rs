//! Native hot-path kernels for the Aevrel data plane.
//!
//! Everything in this module is pure computation: byte-range arithmetic for
//! tensor regions, element-wise reductions, SHA-256 / Merkle folding and
//! source-shard checksum verification. The Python data plane keeps schema
//! validation, identity resolution and I/O orchestration; the numeric kernels
//! live here.
//!
//! Every kernel is exercised against a golden fixture captured from the
//! pre-refactor Python implementation (see `tests/golden.rs`), so the native
//! path is byte-for-byte equivalent to what it replaced.

pub mod hash;
pub mod reduce;
pub mod runs;

pub use hash::{digest_bytes, merkle_root, sha256_bytes, sha256_concat, sha256_file};
pub use reduce::{reduce_bytes, DType};
pub use runs::{byte_runs, coalesce_runs, paired_run_batches, split_runs};

/// Default run-batch ceiling used when a caller does not pass one.
///
/// Mirrors `aevrel.planning.tensor_io.DEFAULT_RUN_BATCH_BYTES` (4 MiB unless a
/// GPU codec is configured); Python always passes an explicit value.
pub const DEFAULT_RUN_BATCH_BYTES: usize = 4 * 1024 * 1024;
