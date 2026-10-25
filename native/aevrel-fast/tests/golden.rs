//! Golden equivalence for the native kernels.
//!
//! `tests/fixtures/golden_kernels.json` was captured from the pre-refactor
//! Python implementation (see `scripts/capture_kernel_golden.py`). Every case
//! in the fixture is replayed through the Rust kernels here and must match
//! byte-for-byte — including the error messages of the rejected inputs.

use aevrel_fast::kernels::hash;
use aevrel_fast::kernels::reduce;
use aevrel_fast::kernels::runs;
use base64::Engine;
use serde_json::{json, Value};
use std::path::PathBuf;

fn fixture() -> Value {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/golden_kernels.json");
    let text = std::fs::read_to_string(&path).expect("read golden fixture");
    serde_json::from_str(&text).expect("parse golden fixture")
}

/// xorshift64*, identical to the generator in `capture_kernel_golden.py`.
fn prng_bytes(seed: u64, size: usize) -> Vec<u8> {
    let mut state = seed;
    let mut out = Vec::with_capacity(size + 8);
    while out.len() < size {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        out.extend_from_slice(&state.to_le_bytes());
    }
    out.truncate(size);
    out
}

fn decode(value: &str) -> Vec<u8> {
    base64::engine::general_purpose::STANDARD
        .decode(value)
        .expect("valid base64 in fixture")
}

fn usize_list(value: &Value) -> Vec<usize> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_u64().expect("unsigned integer") as usize)
        .collect()
}

fn i64_list(value: &Value) -> Vec<i64> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| item.as_i64().expect("signed integer"))
        .collect()
}

fn run_list(value: &Value) -> Vec<(usize, usize)> {
    value
        .as_array()
        .expect("array")
        .iter()
        .map(|item| {
            let pair = item.as_array().expect("run pair");
            (
                pair[0].as_u64().expect("offset") as usize,
                pair[1].as_u64().expect("length") as usize,
            )
        })
        .collect()
}

fn runs_json(runs: &[(usize, usize)]) -> Value {
    Value::Array(
        runs.iter()
            .map(|(offset, length)| json!([offset, length]))
            .collect(),
    )
}

fn batches_json(batches: &[runs::PairedBatch]) -> Value {
    Value::Array(
        batches
            .iter()
            .map(|(target, sources)| json!([[target.0, target.1], runs_json(sources)]))
            .collect(),
    )
}

/// Compare a kernel outcome against the fixture's `result` / `error` pair.
macro_rules! assert_case {
    ($case:expr, $actual:expr) => {{
        let case = $case;
        let actual = $actual;
        match case.get("error") {
            Some(expected) => {
                let error = actual.expect_err("kernel must reject this input");
                assert_eq!(
                    format!("TensorIOError: {error}"),
                    expected.as_str().expect("error string"),
                    "case {}",
                    case["name"]
                );
            }
            None => {
                let expected = case
                    .get("result")
                    .or_else(|| case.get("result_b64"))
                    .expect("result or error");
                assert_eq!(
                    &actual.expect("kernel must accept this input"),
                    expected,
                    "case {}",
                    case["name"]
                );
            }
        }
    }};
}

#[test]
fn byte_runs_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["byte_runs"].as_array().unwrap() {
        let shape = usize_list(&case["shape"]);
        let offset = i64_list(&case["offset"]);
        let region = i64_list(&case["region"]);
        let item_size = case["item_size"].as_u64().unwrap() as usize;
        let actual =
            runs::byte_runs(&shape, &offset, &region, item_size).map(|runs| runs_json(&runs));
        assert_case!(case, actual);
    }
}

#[test]
fn coalesce_runs_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["coalesce_runs"].as_array().unwrap() {
        let input = run_list(&case["runs"]);
        let max_bytes = case
            .get("max_bytes")
            .map(|value| value.as_u64().unwrap() as usize)
            .unwrap_or(4 * 1024 * 1024);
        let actual: Result<Value, String> = Ok(runs_json(&runs::coalesce_runs(&input, max_bytes)));
        assert_case!(case, actual);
    }
}

#[test]
fn split_runs_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["split_runs"].as_array().unwrap() {
        let input = run_list(&case["runs"]);
        let max_bytes = case["max_bytes"].as_u64().unwrap() as usize;
        let actual = runs::split_runs(&input, max_bytes).map(|runs| runs_json(&runs));
        assert_case!(case, actual);
    }
}

#[test]
fn paired_run_batches_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["paired_run_batches"].as_array().unwrap() {
        let target = run_list(&case["target"]);
        let sources: Vec<Vec<(usize, usize)>> = case["sources"]
            .as_array()
            .unwrap()
            .iter()
            .map(run_list)
            .collect();
        let max_bytes = case
            .get("max_bytes")
            .map(|value| value.as_u64().unwrap() as usize)
            .unwrap_or(4 * 1024 * 1024);
        let actual = runs::paired_run_batches(&target, &sources, max_bytes)
            .map(|batches| batches_json(&batches));
        assert_case!(case, actual);
    }
}

#[test]
fn reduce_bytes_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["reduce_bytes"].as_array().unwrap() {
        let inputs: Vec<Vec<u8>> = case["inputs_b64"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| decode(value.as_str().unwrap()))
            .collect();
        let buffers: Vec<&[u8]> = inputs.iter().map(Vec::as_slice).collect();
        let actual = reduce::reduce_bytes(
            &buffers,
            case["op"].as_str().unwrap(),
            case["dtype"].as_str().unwrap(),
        )
        .map(|bytes| Value::String(base64::engine::general_purpose::STANDARD.encode(bytes)));
        assert_case!(case, actual);
    }
}

#[test]
fn merkle_roots_match_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["merkle_root"].as_array().unwrap() {
        let digests: Vec<String> = case["digests"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_string())
            .collect();
        let references: Vec<&str> = digests.iter().map(String::as_str).collect();
        let actual = hash::merkle_root(&references).map(Value::String);
        assert_case!(case, actual);
    }
}

#[test]
fn digest_bytes_matches_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["digest_bytes"].as_array().unwrap() {
        let data = decode(case["data_b64"].as_str().unwrap());
        let actual: Result<Value, String> = Ok(Value::String(hash::digest_bytes(&data)));
        assert_case!(case, actual);
    }
}

#[test]
fn sha256_bytes_matches_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["sha256_bytes"].as_array().unwrap() {
        let data = decode(case["data_b64"].as_str().unwrap());
        let actual: Result<Value, String> = Ok(Value::String(hash::sha256_bytes(&data)));
        assert_case!(case, actual);
    }
}

#[test]
fn sha256_concat_matches_the_golden_fixture() {
    let fixture = fixture();
    for case in fixture["cases"]["sha256_concat"].as_array().unwrap() {
        let data = decode(case["data_b64"].as_str().unwrap());
        let chunk_bytes = case["chunk_bytes"].as_u64().unwrap() as usize;
        let mut chunks: Vec<&[u8]> = Vec::new();
        let mut cursor = 0;
        while cursor < data.len() {
            let end = (cursor + chunk_bytes).min(data.len());
            chunks.push(&data[cursor..end]);
            cursor = end;
        }
        let actual: Result<Value, String> = Ok(Value::String(hash::sha256_concat(&chunks)));
        assert_case!(case, actual);
    }
}

#[test]
fn sha256_file_matches_the_golden_fixture() {
    let fixture = fixture();
    let directory = std::env::temp_dir().join("aevrel-golden-sha256");
    std::fs::create_dir_all(&directory).expect("create temp dir");
    for case in fixture["cases"]["sha256_file"].as_array().unwrap() {
        let seed = case["seed"].as_u64().unwrap();
        let size = case["size"].as_u64().unwrap() as usize;
        let path = directory.join(format!("payload-{seed}-{size}.bin"));
        std::fs::write(&path, prng_bytes(seed, size)).expect("write payload");
        let actual = hash::sha256_file(&path).map(Value::String);
        assert_case!(case, actual);
        let _ = std::fs::remove_file(&path);
    }
    let _ = std::fs::remove_dir_all(&directory);
}

/// The fixture *is* the contract, so its coverage is pinned. A group that
/// silently loses cases would otherwise shrink what the kernels are compared
/// against without failing anything.
#[test]
fn fixture_coverage_is_pinned() {
    let fixture = fixture();
    let expected = [
        ("byte_runs", 13),
        ("coalesce_runs", 10),
        ("split_runs", 6),
        ("paired_run_batches", 8),
        ("reduce_bytes", 146),
        ("merkle_root", 10),
        ("digest_bytes", 4),
        ("sha256_file", 6),
        ("sha256_bytes", 5),
        ("sha256_concat", 4),
    ];
    let cases = fixture["cases"].as_object().unwrap();
    assert_eq!(cases.len(), expected.len(), "unexpected kernel groups");
    for (group, count) in expected {
        let actual = cases
            .get(group)
            .unwrap_or_else(|| panic!("missing group {group}"))
            .as_array()
            .unwrap()
            .len();
        assert_eq!(actual, count, "case count for {group}");
    }
}

/// float16 `sum` resolves NaN differently depending on how many output
/// elements the reduction produces — numpy's pairwise fast path versus its
/// element-wise loop — so both shapes have to stay covered.
#[test]
fn fixture_covers_both_float16_sum_shapes() {
    let fixture = fixture();
    let names: Vec<String> = fixture["cases"]["reduce_bytes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| case["name"].as_str().unwrap().to_string())
        .collect();
    for required in [
        "float16-sum-nan-order-len1",
        "float16-sum-nan-order-len1024",
        "float16-sum-pairwise-9x1",
        "float16-sum-pairwise-130x1",
    ] {
        assert!(
            names.iter().any(|name| name == required),
            "missing {required}"
        );
    }
}
