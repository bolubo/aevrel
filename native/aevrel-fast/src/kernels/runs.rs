//! Byte-range arithmetic for tensor regions.
//!
//! These kernels turn a `(shape, offset, region, item_size)` description into
//! the list of `(byte_offset, byte_length)` runs that a migration node has to
//! read from each source shard and write into the target file. The arithmetic
//! is pure integer math, so the native and the retired Python implementation
//! agree exactly (see `tests/golden.rs`).

/// A half-open byte range `[offset, offset + length)`.
pub type Run = (usize, usize);

/// A target run paired with the source runs that fill it.
pub type PairedBatch = (Run, Vec<Run>);

/// Compute the byte runs covering `region_size` inside `local_shape`.
///
/// `local_offset` is the region's origin expressed in the shard's local
/// coordinates. Rows are walked in row-major order over the outer dimensions;
/// the innermost dimension is contiguous, so one run is emitted per outer
/// index. Offsets and lengths are signed so the kernel can reject negative
/// origins itself instead of leaving that check to the caller.
pub fn byte_runs(
    local_shape: &[usize],
    local_offset: &[i64],
    region_size: &[i64],
    item_size: usize,
) -> Result<Vec<Run>, String> {
    if local_shape.len() != local_offset.len() || local_shape.len() != region_size.len() {
        return Err("tensor region rank mismatch".to_string());
    }
    if region_size.is_empty() {
        return Err("tensor region must have at least one dimension".to_string());
    }
    if region_size.iter().any(|length| *length <= 0) {
        return Ok(Vec::new());
    }
    for ((offset, length), dimension) in local_offset
        .iter()
        .zip(region_size.iter())
        .zip(local_shape.iter())
    {
        let end = i64::try_from(*dimension).unwrap_or(i64::MAX);
        if *offset < 0 || *length < 0 || offset.saturating_add(*length) > end {
            return Err("tensor region is outside local shape".to_string());
        }
    }

    let offset: Vec<usize> = local_offset.iter().map(|value| *value as usize).collect();
    let region: Vec<usize> = region_size.iter().map(|value| *value as usize).collect();
    let shape = local_shape;

    let mut strides = vec![1usize; shape.len()];
    for index in (0..shape.len().saturating_sub(1)).rev() {
        strides[index] = strides[index + 1] * shape[index + 1];
    }

    let run_size = region[region.len() - 1] * item_size;
    let outer_sizes = &region[..region.len() - 1];
    // Suffix products give the mixed-radix stride of every outer dimension, so
    // the last outer dimension varies fastest (row-major, like itertools.product).
    let mut suffix = vec![1usize; outer_sizes.len() + 1];
    for index in (0..outer_sizes.len()).rev() {
        suffix[index] = suffix[index + 1] * outer_sizes[index];
    }
    let outer_total: usize = outer_sizes.iter().product();
    let mut runs = Vec::with_capacity(outer_total);
    let mut coordinates = vec![0usize; outer_sizes.len()];
    for _ in 0..outer_total {
        let mut element_offset = offset[offset.len() - 1];
        for (dim, coordinate) in coordinates.iter().enumerate() {
            element_offset += (offset[dim] + coordinate) * strides[dim];
        }
        runs.push((element_offset * item_size, run_size));
        // Odometer increment: advance the last outer dimension first.
        for dim in (0..coordinates.len()).rev() {
            coordinates[dim] += 1;
            if coordinates[dim] < outer_sizes[dim] {
                break;
            }
            coordinates[dim] = 0;
        }
    }
    Ok(runs)
}

/// Merge adjacent runs, never exceeding `max_bytes` per merged run.
pub fn coalesce_runs(runs: &[Run], max_bytes: usize) -> Vec<Run> {
    let mut result: Vec<Run> = Vec::with_capacity(runs.len());
    for (offset, length) in runs.iter().copied() {
        if let Some((previous_offset, previous_length)) = result.last_mut() {
            let contiguous = previous_offset
                .checked_add(*previous_length)
                .map(|end| end == offset)
                .unwrap_or(false);
            if contiguous && previous_length.saturating_add(length) <= max_bytes {
                *previous_length += length;
                continue;
            }
        }
        result.push((offset, length));
    }
    result
}

/// Split runs so that no run exceeds `max_bytes`.
pub fn split_runs(runs: &[Run], max_bytes: usize) -> Result<Vec<Run>, String> {
    if max_bytes == 0 {
        return Err("run batch size must be positive".to_string());
    }
    let mut result = Vec::new();
    for (offset, length) in runs.iter().copied() {
        if length == 0 {
            continue;
        }
        let mut remaining = length;
        let mut cursor = offset;
        while remaining > 0 {
            let chunk = remaining.min(max_bytes);
            result.push((cursor, chunk));
            cursor += chunk;
            remaining -= chunk;
        }
    }
    Ok(result)
}

/// Pair every target run with the matching source runs and coalesce them into
/// batches that stay adjacent on *both* sides.
pub fn paired_run_batches(
    target_runs: &[Run],
    source_run_lists: &[Vec<Run>],
    max_bytes: usize,
) -> Result<Vec<PairedBatch>, String> {
    let target_list = split_runs(target_runs, max_bytes)?;
    let mut source_lists = Vec::with_capacity(source_run_lists.len());
    for runs in source_run_lists {
        let split = split_runs(runs, max_bytes)?;
        if split.len() != target_list.len() {
            return Err("target and source run counts differ".to_string());
        }
        source_lists.push(split);
    }

    let mut batches: Vec<(Run, Vec<Run>)> = Vec::new();
    let mut target_start = 0;
    while target_start < target_list.len() {
        let mut target_end = target_start + 1;
        let (target_offset, mut target_length) = target_list[target_start];
        let source_offsets: Vec<usize> = source_lists
            .iter()
            .map(|runs| runs[target_start].0)
            .collect();
        let mut source_lengths: Vec<usize> = source_lists
            .iter()
            .map(|runs| runs[target_start].1)
            .collect();
        while target_end < target_list.len() {
            let (next_target_offset, next_target_length) = target_list[target_end];
            let contiguous = target_offset
                .checked_add(target_length)
                .map(|end| end == next_target_offset)
                .unwrap_or(false);
            if !contiguous {
                break;
            }
            let sources_aligned = source_lists.iter().enumerate().all(|(index, runs)| {
                runs[target_end].0 == source_offsets[index] + source_lengths[index]
            });
            if !sources_aligned {
                break;
            }
            if target_length + next_target_length > max_bytes {
                break;
            }
            target_length += next_target_length;
            for (index, runs) in source_lists.iter().enumerate() {
                source_lengths[index] += runs[target_end].1;
            }
            target_end += 1;
        }
        let paired = source_offsets.into_iter().zip(source_lengths).collect();
        batches.push(((target_offset, target_length), paired));
        target_start = target_end;
    }
    Ok(batches)
}

/// Convenience view used by the golden tests and the CLI.
pub fn runs_to_json(runs: &[Run]) -> String {
    let rendered = runs
        .iter()
        .map(|(offset, length)| format!("[{offset},{length}]"))
        .collect::<Vec<_>>()
        .join(",");
    format!("[{rendered}]")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn byte_runs_walks_rows_in_order() {
        let runs = byte_runs(&[4, 4], &[0, 0], &[3, 2], 4).unwrap();
        assert_eq!(runs, vec![(0, 8), (16, 8), (32, 8)]);
    }

    #[test]
    fn byte_runs_applies_offsets_and_strides() {
        let runs = byte_runs(&[4, 4], &[1, 1], &[2, 2], 2).unwrap();
        assert_eq!(runs, vec![(10, 4), (18, 4)]);
    }

    #[test]
    fn byte_runs_empty_region_yields_nothing() {
        assert!(byte_runs(&[6], &[0], &[0], 4).unwrap().is_empty());
        assert!(byte_runs(&[2, 2], &[0, 0], &[2, 0], 4).unwrap().is_empty());
    }

    #[test]
    fn byte_runs_rejects_rank_mismatch() {
        let error = byte_runs(&[2, 2], &[0], &[2, 2], 4).unwrap_err();
        assert_eq!(error, "tensor region rank mismatch");
    }

    #[test]
    fn byte_runs_rejects_out_of_bounds() {
        let error = byte_runs(&[2, 2], &[1, 0], &[2, 2], 4).unwrap_err();
        assert_eq!(error, "tensor region is outside local shape");
    }

    #[test]
    fn coalesce_merges_adjacent_runs() {
        assert_eq!(
            coalesce_runs(&[(0, 4), (4, 4), (8, 4)], 4 * 1024 * 1024),
            vec![(0, 12)]
        );
        assert_eq!(
            coalesce_runs(&[(0, 4), (4, 4), (8, 4)], 8),
            vec![(0, 8), (8, 4)]
        );
        assert_eq!(
            coalesce_runs(&[(0, 4), (100, 4), (104, 4)], 4 * 1024 * 1024),
            vec![(0, 4), (100, 8)]
        );
    }

    #[test]
    fn coalesce_absorbs_zero_length_runs() {
        assert_eq!(
            coalesce_runs(&[(0, 4), (4, 0), (4, 4)], 4 * 1024 * 1024),
            vec![(0, 8)]
        );
    }

    #[test]
    fn split_runs_enforces_the_ceiling() {
        // Runs already below the ceiling are passed through untouched.
        assert_eq!(
            split_runs(&[(0, 10), (10, 10), (20, 10)], 16).unwrap(),
            vec![(0, 10), (10, 10), (20, 10)]
        );
        assert_eq!(
            split_runs(&[(0, 100)], 7).unwrap(),
            vec![
                (0, 7),
                (7, 7),
                (14, 7),
                (21, 7),
                (28, 7),
                (35, 7),
                (42, 7),
                (49, 7),
                (56, 7),
                (63, 7),
                (70, 7),
                (77, 7),
                (84, 7),
                (91, 7),
                (98, 2),
            ]
        );
        assert_eq!(split_runs(&[(0, 0), (4, 4)], 8).unwrap(), vec![(4, 4)]);
        assert_eq!(
            split_runs(&[(0, 4)], 0).unwrap_err(),
            "run batch size must be positive"
        );
    }

    #[test]
    fn paired_batches_align_target_and_sources() {
        let batches = paired_run_batches(
            &[(0, 12), (12, 12)],
            &[vec![(0, 12), (12, 12)], vec![(100, 12), (112, 12)]],
            4 * 1024 * 1024,
        )
        .unwrap();
        assert_eq!(batches, vec![((0, 24), vec![(0, 24), (100, 24)])]);
    }

    #[test]
    fn paired_batches_break_on_source_gaps() {
        let batches = paired_run_batches(
            &[(0, 8), (8, 8), (16, 8)],
            &[
                vec![(0, 8), (8, 8), (16, 8)],
                vec![(0, 8), (20, 8), (28, 8)],
            ],
            4 * 1024 * 1024,
        )
        .unwrap();
        assert_eq!(
            batches,
            vec![
                ((0, 8), vec![(0, 8), (0, 8)]),
                ((8, 16), vec![(8, 16), (20, 16)]),
            ]
        );
    }

    #[test]
    fn paired_batches_reject_count_mismatch() {
        let error = paired_run_batches(
            &[(0, 10), (10, 10)],
            &[vec![(0, 10), (10, 10)], vec![(0, 20)]],
            4 * 1024 * 1024,
        )
        .unwrap_err();
        assert_eq!(error, "target and source run counts differ");
    }
}
