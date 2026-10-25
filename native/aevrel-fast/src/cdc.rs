//! Content-defined / fixed / whole-file chunking with SHA-256 CAS ingest.
//!
//! This is the single chunking implementation for Aevrel: the Python data
//! plane shells out to the `aevrel-fast` binary built from this crate, and
//! there is deliberately no Python fallback.
//!
//! The chunk boundaries, digests and manifest layout are an on-disk contract,
//! so the gear table and the "empty file yields one zero-length chunk" rule
//! must stay byte-for-byte compatible with the reference implementation.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, File};
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;

const DEFAULT_CHUNK_BYTES: usize = 4 * 1024 * 1024;
const READ_BUFFER_BYTES: usize = 8 * 1024 * 1024;

#[derive(Serialize)]
struct Chunk {
    digest: String,
    size: u64,
    file: String,
    offset: u64,
    length: u64,
}

#[derive(Deserialize)]
struct FileEntry {
    file: String,
    path: String,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum ChunkAlgorithm {
    File,
    Fixed,
    Cdc,
}

impl ChunkAlgorithm {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "file" => Some(Self::File),
            "fixed" => Some(Self::Fixed),
            "cdc" => Some(Self::Cdc),
            _ => None,
        }
    }
}

/// Gear table for content-defined chunking.
///
/// Entry `i` is the first 8 bytes of `SHA256([i])` interpreted big-endian.
/// This table is part of the on-disk contract: chunk boundaries (and therefore
/// chunk digests and manifest roots) must stay stable across implementations,
/// so it is derived from SHA-256 exactly like the reference table.
fn gear_table() -> [u64; 256] {
    let mut table = [0u64; 256];
    for (value, slot) in table.iter_mut().enumerate() {
        let digest = Sha256::digest([value as u8]);
        *slot = u64::from_be_bytes(digest[..8].try_into().expect("sha256 digest is 32 bytes"));
    }
    table
}

struct Chunking {
    algorithm: ChunkAlgorithm,
    chunk_bytes: usize,
    min_bytes: usize,
    avg_bytes: usize,
    max_bytes: usize,
}

impl Chunking {
    fn mask(&self) -> u64 {
        // `bit_width` is `BITS - leading_zeros`; keeping the comparison in u64
        // avoids mixing the width of `usize` with the width of `avg_bytes`.
        let mask_bits = ((self.avg_bytes as u64).bit_width() - 1).max(1);
        if mask_bits >= 64 {
            u64::MAX
        } else {
            (1u64 << mask_bits) - 1
        }
    }
}

pub fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut cas = None;
    let mut root = None;
    let mut file_map = None;
    let mut chunking = Chunking {
        algorithm: ChunkAlgorithm::File,
        chunk_bytes: DEFAULT_CHUNK_BYTES,
        min_bytes: 32 * 1024,
        avg_bytes: 128 * 1024,
        max_bytes: 512 * 1024,
    };
    let mut workers: Option<usize> = None;
    let mut hardlink = true;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--cas" => cas = args.next(),
            "--root" => root = args.next(),
            "--file-map" => file_map = args.next(),
            "--chunk-alg" => {
                let value = args.next().ok_or("missing --chunk-alg")?;
                chunking.algorithm = ChunkAlgorithm::parse(&value)
                    .ok_or_else(|| format!("unsupported chunk algorithm: {value}"))?;
            }
            "--chunk-bytes" => chunking.chunk_bytes = parse_usize(args.next(), "--chunk-bytes")?,
            "--min-bytes" => chunking.min_bytes = parse_usize(args.next(), "--min-bytes")?,
            "--avg-bytes" => chunking.avg_bytes = parse_usize(args.next(), "--avg-bytes")?,
            "--max-bytes" => chunking.max_bytes = parse_usize(args.next(), "--max-bytes")?,
            "--workers" => workers = Some(parse_usize(args.next(), "--workers")?),
            "--copy" => hardlink = false,
            _ => return Err(format!("unknown argument: {arg}").into()),
        }
    }
    match chunking.algorithm {
        ChunkAlgorithm::File => {}
        ChunkAlgorithm::Fixed => {
            if chunking.chunk_bytes < 1 {
                return Err("invalid fixed chunk size".into());
            }
        }
        ChunkAlgorithm::Cdc => {
            if chunking.min_bytes < 1
                || chunking.max_bytes < chunking.min_bytes
                || chunking.avg_bytes < chunking.min_bytes
            {
                return Err("invalid CDC chunk bounds".into());
            }
        }
    }
    let cas = PathBuf::from(cas.ok_or("--cas is required")?);
    fs::create_dir_all(&cas)?;

    let entries = if let Some(path) = file_map {
        let payload: Vec<FileEntry> = serde_json::from_slice(&fs::read(path)?)?;
        payload
            .into_iter()
            .map(|entry| (entry.file, PathBuf::from(entry.path)))
            .collect::<Vec<_>>()
    } else {
        let mut entries = Vec::new();
        let root = PathBuf::from(root.ok_or("--root or --file-map is required")?);
        collect_files(&root, Path::new(""), &mut entries)?;
        entries
    };

    let total_size = entries
        .iter()
        .filter_map(|(_, path)| fs::metadata(path).ok())
        .map(|metadata| metadata.len())
        .sum::<u64>();
    let average_size = if entries.is_empty() {
        0
    } else {
        total_size / entries.len() as u64
    };
    let workers = workers
        .unwrap_or(if average_size < 8 * 1024 * 1024 { 8 } else { 1 })
        .max(1)
        .min(entries.len().max(1));
    let next = AtomicUsize::new(0);
    let (sender, receiver) = mpsc::channel::<(usize, Result<Vec<Chunk>, String>)>();
    std::thread::scope(|scope| {
        for _ in 0..workers {
            let sender = sender.clone();
            let entries = &entries;
            let cas = &cas;
            let next = &next;
            let chunking = &chunking;
            scope.spawn(move || loop {
                let index = next.fetch_add(1, Ordering::Relaxed);
                if index >= entries.len() {
                    break;
                }
                let (name, path) = &entries[index];
                let result = ingest_file(path, name, cas, hardlink, chunking)
                    .map_err(|error| error.to_string());
                let _ = sender.send((index, result));
            });
        }
    });
    drop(sender);
    let mut results: Vec<Vec<Chunk>> = (0..entries.len()).map(|_| Vec::new()).collect();
    for (index, result) in receiver {
        results[index] = result.map_err(io::Error::other)?;
    }
    let mut chunks = results.into_iter().flatten().collect::<Vec<_>>();
    chunks.sort_by(|a, b| {
        (&a.file, a.offset, a.length, &a.digest).cmp(&(&b.file, b.offset, b.length, &b.digest))
    });
    let output = serde_json::json!({"chunks": chunks});
    let stdout = io::stdout();
    let mut handle = stdout.lock();
    serde_json::to_writer(&mut handle, &output)?;
    writeln!(handle)?;
    Ok(())
}

fn parse_usize(value: Option<String>, flag: &str) -> Result<usize, Box<dyn std::error::Error>> {
    value
        .ok_or_else(|| format!("missing {flag}").into())
        .and_then(|value| {
            value
                .parse::<usize>()
                .map_err(|error| format!("{flag}: {error}").into())
        })
}

fn collect_files(
    root: &Path,
    relative: &Path,
    entries: &mut Vec<(String, PathBuf)>,
) -> io::Result<()> {
    let directory = root.join(relative);
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let child = relative.join(entry.file_name());
        if child
            .components()
            .any(|part| part.as_os_str().to_string_lossy().starts_with(".aevrel"))
        {
            continue;
        }
        if file_type.is_dir() {
            collect_files(root, &child, entries)?;
        } else if file_type.is_file() {
            entries.push((
                child.to_string_lossy().replace('\\', "/"),
                root.join(&child),
            ));
        }
    }
    Ok(())
}

/// Ingest one file into the content-addressed store and return its chunks.
///
/// A file that yields no chunks (an empty file under fixed/CDC chunking) still
/// contributes a single zero-length chunk, matching the reference contract.
fn ingest_file(
    source: &Path,
    name: &str,
    cas: &Path,
    hardlink: bool,
    chunking: &Chunking,
) -> Result<Vec<Chunk>, Box<dyn std::error::Error>> {
    let chunks = match chunking.algorithm {
        ChunkAlgorithm::File => {
            let (digest, size) = ingest_whole(source, cas, hardlink)?;
            vec![Chunk {
                digest,
                size,
                file: name.to_string(),
                offset: 0,
                length: size,
            }]
        }
        ChunkAlgorithm::Fixed => ingest_fixed(source, name, cas, chunking.chunk_bytes)?,
        ChunkAlgorithm::Cdc => ingest_cdc(source, name, cas, chunking)?,
    };
    if chunks.is_empty() {
        let (digest, _) = store_chunk(cas, b"")?;
        return Ok(vec![Chunk {
            digest,
            size: 0,
            file: name.to_string(),
            offset: 0,
            length: 0,
        }]);
    }
    Ok(chunks)
}

fn ingest_whole(
    source: &Path,
    cas: &Path,
    hardlink: bool,
) -> Result<(String, u64), Box<dyn std::error::Error>> {
    let size = fs::metadata(source)?.len();
    let mut hasher = Sha256::new();
    let mut reader = BufReader::with_capacity(READ_BUFFER_BYTES, File::open(source)?);
    let mut buffer = vec![0u8; READ_BUFFER_BYTES];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    let (digest, hex) = digest_hex(hasher);
    let destination = cas_path(cas, &hex);
    if destination.is_file() {
        let existing = fs::metadata(&destination)?.len();
        if existing != size {
            return Err(format!(
                "existing CAS chunk has wrong size: {} != {}",
                existing, size
            )
            .into());
        }
        return Ok((digest, size));
    }
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    if hardlink && fs::hard_link(source, &destination).is_ok() {
        return Ok((digest, size));
    }
    fs::copy(source, &destination)?;
    Ok((digest, size))
}

fn ingest_fixed(
    source: &Path,
    name: &str,
    cas: &Path,
    chunk_bytes: usize,
) -> Result<Vec<Chunk>, Box<dyn std::error::Error>> {
    let mut reader = BufReader::with_capacity(READ_BUFFER_BYTES, File::open(source)?);
    let mut buffer = vec![0u8; READ_BUFFER_BYTES];
    let mut chunks = Vec::new();
    let mut offset = 0u64;
    let mut pending: Vec<u8> = Vec::with_capacity(chunk_bytes);
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        pending.extend_from_slice(&buffer[..count]);
        while pending.len() >= chunk_bytes {
            let block: Vec<u8> = pending.drain(..chunk_bytes).collect();
            let (digest, size) = store_chunk(cas, &block)?;
            chunks.push(Chunk {
                digest,
                size,
                file: name.to_string(),
                offset,
                length: size,
            });
            offset += size;
        }
    }
    if !pending.is_empty() {
        let (digest, size) = store_chunk(cas, &pending)?;
        chunks.push(Chunk {
            digest,
            size,
            file: name.to_string(),
            offset,
            length: size,
        });
    }
    Ok(chunks)
}

fn ingest_cdc(
    source: &Path,
    name: &str,
    cas: &Path,
    chunking: &Chunking,
) -> Result<Vec<Chunk>, Box<dyn std::error::Error>> {
    let gear = gear_table();
    let mask = chunking.mask();
    let mut reader = BufReader::with_capacity(READ_BUFFER_BYTES, File::open(source)?);
    let mut buffer = vec![0u8; READ_BUFFER_BYTES];
    let mut chunk: Vec<u8> = Vec::with_capacity(chunking.max_bytes);
    let mut chunks = Vec::new();
    let mut offset = 0u64;
    let mut rolling = 0u64;
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        for &byte in &buffer[..count] {
            chunk.push(byte);
            rolling = (rolling << 1).wrapping_add(gear[byte as usize]);
            let length = chunk.len();
            if (length >= chunking.min_bytes && (rolling & mask) == 0)
                || length >= chunking.max_bytes
            {
                let (digest, size) = store_chunk(cas, &chunk)?;
                chunks.push(Chunk {
                    digest,
                    size,
                    file: name.to_string(),
                    offset,
                    length: size,
                });
                offset += size;
                chunk.clear();
                rolling = 0;
            }
        }
    }
    if !chunk.is_empty() {
        let (digest, size) = store_chunk(cas, &chunk)?;
        chunks.push(Chunk {
            digest,
            size,
            file: name.to_string(),
            offset,
            length: size,
        });
    }
    Ok(chunks)
}

fn store_chunk(cas: &Path, data: &[u8]) -> Result<(String, u64), Box<dyn std::error::Error>> {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let (digest, hex) = digest_hex(hasher);
    let destination = cas_path(cas, &hex);
    let size = data.len() as u64;
    if destination.is_file() {
        let existing = fs::metadata(&destination)?.len();
        if existing != size {
            return Err(format!(
                "existing CAS chunk has wrong size: {} != {}",
                existing, size
            )
            .into());
        }
        return Ok((digest, size));
    }
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(destination, data)?;
    Ok((digest, size))
}

fn digest_hex(hasher: Sha256) -> (String, String) {
    let hex = format!("{:x}", hasher.finalize());
    (format!("sha256:{hex}"), hex)
}

fn cas_path(cas: &Path, hex: &str) -> PathBuf {
    cas.join("sha256").join(&hex[..2]).join(&hex[2..])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!("aevrel-fast-test-{tag}"));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).expect("create temp dir");
        path
    }

    #[test]
    fn gear_table_is_sha256_derived_and_distinct() {
        let table = gear_table();
        // The table is the first 8 bytes of SHA256([i]) big-endian; verify it
        // against an independent sha2 computation for every entry.
        for (value, entry) in table.iter().enumerate() {
            let digest = Sha256::digest([value as u8]);
            let expected = u64::from_be_bytes(digest[..8].try_into().unwrap());
            assert_eq!(*entry, expected, "gear table mismatch at {value}");
        }
        let unique: std::collections::HashSet<u64> = table.iter().copied().collect();
        assert_eq!(unique.len(), 256, "gear table entries must be distinct");
    }

    #[test]
    fn gear_table_is_stable() {
        assert_eq!(gear_table(), gear_table());
    }

    #[test]
    fn fixed_chunking_splits_on_boundaries() {
        let dir = temp_dir("fixed");
        let cas = dir.join("cas");
        let source = dir.join("data.bin");
        fs::write(&source, b"0123456789").unwrap();
        let chunking = Chunking {
            algorithm: ChunkAlgorithm::Fixed,
            chunk_bytes: 4,
            min_bytes: 1,
            avg_bytes: 1,
            max_bytes: 4,
        };
        let chunks = ingest_file(&source, "data.bin", &cas, false, &chunking).unwrap();
        let lengths: Vec<u64> = chunks.iter().map(|chunk| chunk.length).collect();
        assert_eq!(lengths, vec![4, 4, 2]);
        assert_eq!(chunks.last().unwrap().offset, 8);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn fixed_chunking_empty_file_yields_empty_chunk() {
        let dir = temp_dir("fixed-empty");
        let cas = dir.join("cas");
        let source = dir.join("empty.bin");
        fs::write(&source, b"").unwrap();
        let chunking = Chunking {
            algorithm: ChunkAlgorithm::Fixed,
            chunk_bytes: 4,
            min_bytes: 1,
            avg_bytes: 1,
            max_bytes: 4,
        };
        let chunks = ingest_file(&source, "empty.bin", &cas, false, &chunking).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].size, 0);
        assert_eq!(chunks[0].length, 0);
        assert_eq!(
            chunks[0].digest,
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn cdc_with_min_equal_to_max_is_uniform() {
        let dir = temp_dir("cdc-uniform");
        let cas = dir.join("cas");
        let source = dir.join("data.bin");
        fs::write(&source, vec![7u8; 10_000]).unwrap();
        let chunking = Chunking {
            algorithm: ChunkAlgorithm::Cdc,
            chunk_bytes: DEFAULT_CHUNK_BYTES,
            min_bytes: 1024,
            avg_bytes: 1024,
            max_bytes: 1024,
        };
        let chunks = ingest_file(&source, "data.bin", &cas, false, &chunking).unwrap();
        assert_eq!(chunks.len(), 10);
        assert!(chunks[..9].iter().all(|chunk| chunk.length == 1024));
        let last = chunks.last().unwrap();
        assert_eq!(last.length, 784);
        assert_eq!(last.offset, 9 * 1024);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn cdc_empty_file_yields_empty_chunk() {
        let dir = temp_dir("cdc-empty");
        let cas = dir.join("cas");
        let source = dir.join("empty.bin");
        fs::write(&source, b"").unwrap();
        let chunking = Chunking {
            algorithm: ChunkAlgorithm::Cdc,
            chunk_bytes: DEFAULT_CHUNK_BYTES,
            min_bytes: 1024,
            avg_bytes: 4096,
            max_bytes: 8192,
        };
        let chunks = ingest_file(&source, "empty.bin", &cas, false, &chunking).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].size, 0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn file_chunking_is_whole_file() {
        let dir = temp_dir("file");
        let cas = dir.join("cas");
        let source = dir.join("data.bin");
        fs::write(&source, b"whole-file-chunk").unwrap();
        let chunking = Chunking {
            algorithm: ChunkAlgorithm::File,
            chunk_bytes: DEFAULT_CHUNK_BYTES,
            min_bytes: 1,
            avg_bytes: 1,
            max_bytes: 1,
        };
        let chunks = ingest_file(&source, "data.bin", &cas, false, &chunking).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0].length, 16);
        assert_eq!(chunks[0].offset, 0);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn chunk_algorithm_parsing_rejects_unknown() {
        assert!(ChunkAlgorithm::parse("bogus").is_none());
        assert_eq!(ChunkAlgorithm::parse("fixed"), Some(ChunkAlgorithm::Fixed));
        assert_eq!(ChunkAlgorithm::parse("cdc"), Some(ChunkAlgorithm::Cdc));
        assert_eq!(ChunkAlgorithm::parse("file"), Some(ChunkAlgorithm::File));
    }
}
