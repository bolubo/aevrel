//! `aevrel_fast` — in-process hot-path kernels for the Aevrel data plane.
//!
//! The Python data plane loads this extension module (see
//! `src/aevrel/fastkernels.py`) and calls the kernels directly, so per-call
//! process spawn overhead never shows up in the planner, the executor or the
//! proof builder. The CLI binary in `main.rs` shares the same kernels.
//!
//! Kernel errors surface as `ValueError` carrying the exact message the retired
//! Python implementation raised; the Python wrappers re-raise them as the
//! domain-specific `TensorIOError` / `StorageError`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

use crate::kernels::hash;
use crate::kernels::reduce;
use crate::kernels::runs;
use crate::kernels::verify;

/// Byte runs covering a tensor region.
#[pyfunction]
fn byte_runs(
    shape: Vec<usize>,
    offset: Vec<i64>,
    region: Vec<i64>,
    item_size: usize,
) -> PyResult<Vec<(usize, usize)>> {
    runs::byte_runs(&shape, &offset, &region, item_size).map_err(PyValueError::new_err)
}

/// Merge adjacent runs without exceeding `max_bytes`.
#[pyfunction]
fn coalesce_runs(runs: Vec<(usize, usize)>, max_bytes: usize) -> Vec<(usize, usize)> {
    runs::coalesce_runs(&runs, max_bytes)
}

/// Split runs so none exceeds `max_bytes`.
#[pyfunction]
fn split_runs(runs: Vec<(usize, usize)>, max_bytes: usize) -> PyResult<Vec<(usize, usize)>> {
    runs::split_runs(&runs, max_bytes).map_err(PyValueError::new_err)
}

/// Pair target runs with source runs and coalesce both sides together.
#[pyfunction]
#[pyo3(signature = (target, sources, max_bytes))]
fn paired_run_batches(
    target: Vec<(usize, usize)>,
    sources: Vec<Vec<(usize, usize)>>,
    max_bytes: usize,
) -> PyResult<Vec<runs::PairedBatch>> {
    runs::paired_run_batches(&target, &sources, max_bytes).map_err(PyValueError::new_err)
}

/// Element-wise reduction over equally sized byte buffers.
#[pyfunction]
fn reduce_bytes(inputs: Vec<Bound<'_, PyBytes>>, op: &str, dtype: &str) -> PyResult<Vec<u8>> {
    let buffers: Vec<&[u8]> = inputs.iter().map(|value| value.as_bytes()).collect();
    reduce::reduce_bytes(&buffers, op, dtype).map_err(PyValueError::new_err)
}

/// `sha256:<hex>` for an in-memory buffer.
#[pyfunction]
fn digest_bytes(data: &[u8]) -> String {
    hash::digest_bytes(data)
}

/// Fold `sha256:<hex>` digests into one Merkle root.
#[pyfunction]
fn merkle_root(digests: Vec<String>) -> PyResult<String> {
    let references: Vec<&str> = digests.iter().map(String::as_str).collect();
    hash::merkle_root(&references).map_err(PyValueError::new_err)
}

/// Stream-hash a file (1 MiB blocks).
///
/// The GIL is released for the whole scan. Hashing a shard is a long, pure-Rust
/// operation, and holding the GIL through it serialises every other Python
/// thread in the process -- including the other shards of the same
/// verification -- which is why parallel hashing used to show no speedup.
#[pyfunction]
fn sha256_file(py: Python<'_>, path: std::path::PathBuf) -> PyResult<String> {
    py.allow_threads(|| hash::sha256_file(&path))
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Hash an in-memory payload.
#[pyfunction]
fn sha256_bytes(data: &[u8]) -> String {
    hash::sha256_bytes(data)
}

/// Hash a sequence of payloads as if concatenated.
#[pyfunction]
fn sha256_concat(chunks: Vec<Bound<'_, PyBytes>>) -> String {
    let buffers: Vec<&[u8]> = chunks.iter().map(|value| value.as_bytes()).collect();
    hash::sha256_concat(&buffers)
}

/// Verify target bytes against source ranges without crossing into Python.
///
/// `batches` is `[(target_offset, target_size, [(source_offset, source_size)])]`
/// in plan order. Returns the absolute target offset of the first mismatch, or
/// `None` when every batch matches. The GIL is released for the whole scan.
#[pyfunction]
#[pyo3(signature = (target, sources, batches, op, dtype))]
fn verify_batches(
    py: Python<'_>,
    target: std::path::PathBuf,
    sources: Vec<std::path::PathBuf>,
    batches: Vec<verify::BatchSpec>,
    op: &str,
    dtype: &str,
) -> PyResult<Option<u64>> {
    let batches: Vec<verify::Batch> = batches
        .into_iter()
        .map(|(target_offset, target_size, sources)| verify::Batch {
            target_offset,
            target_size,
            sources,
        })
        .collect();
    py.allow_threads(|| verify::verify_batches(&target, &sources, &batches, op, dtype))
        .map_err(PyValueError::new_err)
}

#[pymodule]
fn aevrel_fast(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add(
        "DEFAULT_RUN_BATCH_BYTES",
        crate::kernels::DEFAULT_RUN_BATCH_BYTES,
    )?;
    module.add_function(wrap_pyfunction!(byte_runs, module)?)?;
    module.add_function(wrap_pyfunction!(coalesce_runs, module)?)?;
    module.add_function(wrap_pyfunction!(split_runs, module)?)?;
    module.add_function(wrap_pyfunction!(paired_run_batches, module)?)?;
    module.add_function(wrap_pyfunction!(reduce_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(digest_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(merkle_root, module)?)?;
    module.add_function(wrap_pyfunction!(sha256_file, module)?)?;
    module.add_function(wrap_pyfunction!(sha256_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(sha256_concat, module)?)?;
    module.add_function(wrap_pyfunction!(verify_batches, module)?)?;
    Ok(())
}

pub mod cdc;
pub mod kernels;
