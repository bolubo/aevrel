//! Verify migrated target bytes against their sources without leaving Rust.
//!
//! The retired Python loop read one run at a time: read the source ranges, call
//! `reduce_bytes`, read the target range, then compare with
//! `hmac.compare_digest`. A 64 MiB migration is over a thousand such runs, so it
//! paid a thousand Python calls, three byte-string allocations per call and a
//! constant-time scan that is far slower than a plain memcmp.
//!
//! This kernel does the same work with one file handle per side, reused buffers,
//! the same `reduce_bytes` the writer uses, and a byte-exact memcmp. The
//! predicate is unchanged -- only the path to it is.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use super::reduce::reduce_bytes;

/// Wire format for one batch: `(target_offset, target_size, [(source_offset, source_size)])`.
///
/// A named alias keeps the PyO3 signature readable and keeps clippy's
/// `type_complexity` quiet without an `allow`.
pub type BatchSpec = (u64, u64, Vec<(u64, u64)>);

/// One paired batch: a target range plus the source ranges that fill it.
pub struct Batch {
    /// Absolute byte offset in the target file.
    pub target_offset: u64,
    /// Number of target bytes this batch covers.
    pub target_size: u64,
    /// `(offset, size)` per source input, in plan order. Empty for `zeros`.
    pub sources: Vec<(u64, u64)>,
}

/// Position of the first byte where the target disagrees with the plan.
///
/// `None` means every batch matched. The offset returned is absolute within the
/// target file, so a caller can report it without knowing the batch layout.
pub fn verify_batches(
    target: &Path,
    sources: &[PathBuf],
    batches: &[Batch],
    op: &str,
    dtype: &str,
) -> Result<Option<u64>, String> {
    let target_file = open(target)?;
    let mut source_files = Vec::with_capacity(sources.len());
    for path in sources {
        source_files.push(open(path)?);
    }

    let mut target_buffer: Vec<u8> = Vec::new();
    let mut source_buffers: Vec<Vec<u8>> = (0..source_files.len()).map(|_| Vec::new()).collect();

    for batch in batches {
        target_buffer.resize(batch.target_size as usize, 0);
        read_at(&target_file, batch.target_offset, &mut target_buffer)?;

        if op == "zeros" {
            if let Some(index) = target_buffer.iter().position(|byte| *byte != 0) {
                return Ok(Some(batch.target_offset + index as u64));
            }
            continue;
        }

        if batch.sources.len() != source_files.len() {
            return Err(format!(
                "batch covers {} sources but {} files were opened",
                batch.sources.len(),
                source_files.len()
            ));
        }

        for (index, (slot, source)) in source_buffers.iter_mut().zip(&batch.sources).enumerate() {
            slot.resize(source.1 as usize, 0);
            read_at(&source_files[index], source.0, slot)?;
        }

        // `copy` needs no reduction buffer: the single source is the expectation.
        if op == "copy" && source_buffers.len() == 1 {
            if target_buffer != source_buffers[0] {
                return Ok(Some(
                    batch.target_offset + first_difference(&target_buffer, &source_buffers[0]),
                ));
            }
            continue;
        }

        let refs: Vec<&[u8]> = source_buffers.iter().map(|slot| slot.as_slice()).collect();
        let expected = reduce_bytes(&refs, op, dtype)?;
        if target_buffer != expected {
            return Ok(Some(
                batch.target_offset + first_difference(&target_buffer, &expected),
            ));
        }
    }
    Ok(None)
}

fn open(path: &Path) -> Result<File, String> {
    File::open(path).map_err(|exc| format!("could not open {}: {exc}", path.display()))
}

fn read_at(file: &File, offset: u64, buffer: &mut [u8]) -> Result<(), String> {
    if buffer.is_empty() {
        return Ok(());
    }
    let mut handle = file;
    handle
        .seek(SeekFrom::Start(offset))
        .map_err(|exc| format!("seek to {offset} failed: {exc}"))?;
    handle
        .read_exact(buffer)
        .map_err(|exc| format!("read of {} bytes at {offset} failed: {exc}", buffer.len()))
}

/// First differing index, or the length of the shorter side when sizes differ.
fn first_difference(left: &[u8], right: &[u8]) -> u64 {
    let common = left.len().min(right.len());
    match left[..common]
        .iter()
        .zip(&right[..common])
        .position(|(a, b)| a != b)
    {
        Some(index) => index as u64,
        None => common as u64,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn temp_dir(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("aevrel-verify-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).expect("temp dir");
        root
    }

    fn write(path: &Path, bytes: &[u8]) {
        let mut file = File::create(path).expect("create");
        file.write_all(bytes).expect("write");
    }

    #[test]
    fn matching_copy_batches_pass() {
        let root = temp_dir("copy-ok");
        let source = root.join("source.bin");
        let target = root.join("target.bin");
        let payload: Vec<u8> = (0..64u16).map(|value| value as u8).collect();
        write(&source, &payload);
        write(&target, &payload);
        let batches = vec![Batch {
            target_offset: 0,
            target_size: 64,
            sources: vec![(0, 64)],
        }];
        let outcome = verify_batches(&target, &[source], &batches, "copy", "uint8");
        assert_eq!(outcome, Ok(None));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn a_single_flipped_byte_is_reported_by_absolute_offset() {
        let root = temp_dir("copy-bad");
        let source = root.join("source.bin");
        let target = root.join("target.bin");
        let payload = vec![7u8; 128];
        let mut broken = payload.clone();
        broken[100] = 8;
        write(&source, &payload);
        write(&target, &broken);
        // Second batch starts at 64, so byte 100 is offset 36 inside it.
        let batches = vec![
            Batch {
                target_offset: 0,
                target_size: 64,
                sources: vec![(0, 64)],
            },
            Batch {
                target_offset: 64,
                target_size: 64,
                sources: vec![(64, 64)],
            },
        ];
        let outcome = verify_batches(&target, &[source], &batches, "copy", "uint8");
        assert_eq!(outcome, Ok(Some(100)));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn zeros_batches_check_for_any_nonzero_byte() {
        let root = temp_dir("zeros");
        let target = root.join("target.bin");
        write(&target, &[0u8; 32]);
        let clean = vec![Batch {
            target_offset: 0,
            target_size: 32,
            sources: Vec::new(),
        }];
        assert_eq!(
            verify_batches(&target, &[], &clean, "zeros", "uint8"),
            Ok(None)
        );

        let mut dirty = vec![0u8; 32];
        dirty[17] = 1;
        write(&target, &dirty);
        assert_eq!(
            verify_batches(&target, &[], &clean, "zeros", "uint8"),
            Ok(Some(17))
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn sum_batches_use_the_writer_reduction() {
        let root = temp_dir("sum");
        let first = root.join("a.bin");
        let second = root.join("b.bin");
        let target = root.join("target.bin");
        // Two int64 values per buffer; the sum is what the writer would have stored.
        let left: Vec<u8> = [1i64.to_le_bytes(), 2i64.to_le_bytes()].concat();
        let right: Vec<u8> = [10i64.to_le_bytes(), 20i64.to_le_bytes()].concat();
        let expected: Vec<u8> = [11i64.to_le_bytes(), 22i64.to_le_bytes()].concat();
        write(&first, &left);
        write(&second, &right);
        write(&target, &expected);
        let batches = vec![Batch {
            target_offset: 0,
            target_size: 16,
            sources: vec![(0, 16), (0, 16)],
        }];
        assert_eq!(
            verify_batches(
                &target,
                &[first.clone(), second.clone()],
                &batches,
                "sum",
                "int64"
            ),
            Ok(None)
        );

        let wrong: Vec<u8> = [11i64.to_le_bytes(), 23i64.to_le_bytes()].concat();
        write(&target, &wrong);
        assert_eq!(
            verify_batches(&target, &[first, second], &batches, "sum", "int64"),
            Ok(Some(8))
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn mismatched_source_count_is_an_error_not_a_pass() {
        let root = temp_dir("arity");
        let target = root.join("target.bin");
        write(&target, &[0u8; 8]);
        let batches = vec![Batch {
            target_offset: 0,
            target_size: 8,
            sources: vec![(0, 8)],
        }];
        let outcome = verify_batches(&target, &[], &batches, "copy", "uint8");
        assert!(outcome.is_err(), "expected an arity error, got {outcome:?}");
        let _ = std::fs::remove_dir_all(root);
    }
}
