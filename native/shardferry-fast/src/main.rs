use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, File};
use std::io::{self, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;

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

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut cas = None;
    let mut root = None;
    let mut file_map = None;
    let mut chunk_alg = String::from("file");
    let mut min_bytes = 256 * 1024usize;
    let mut avg_bytes = 1024 * 1024usize;
    let mut max_bytes = 4 * 1024 * 1024usize;
    let mut workers: Option<usize> = None;
    let mut hardlink = true;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--cas" => cas = args.next(),
            "--root" => root = args.next(),
            "--file-map" => file_map = args.next(),
            "--chunk-alg" => chunk_alg = args.next().ok_or("missing --chunk-alg")?,
            "--min-bytes" => min_bytes = parse_usize(args.next(), "--min-bytes")?,
            "--avg-bytes" => avg_bytes = parse_usize(args.next(), "--avg-bytes")?,
            "--max-bytes" => max_bytes = parse_usize(args.next(), "--max-bytes")?,
            "--workers" => workers = Some(parse_usize(args.next(), "--workers")?),
            "--copy" => hardlink = false,
            _ => return Err(format!("unknown argument: {arg}").into()),
        }
    }
    if !matches!(chunk_alg.as_str(), "file" | "cdc") {
        return Err(format!("unsupported chunk algorithm: {chunk_alg}").into());
    }
    if chunk_alg == "cdc" && (min_bytes < 1 || max_bytes < min_bytes || avg_bytes < min_bytes) {
        return Err("invalid CDC chunk bounds".into());
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
        .unwrap_or_else(|| if average_size < 8 * 1024 * 1024 { 8 } else { 1 })
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
            let chunk_alg = chunk_alg.as_str();
            scope.spawn(move || loop {
                let index = next.fetch_add(1, Ordering::Relaxed);
                if index >= entries.len() {
                    break;
                }
                let (name, path) = &entries[index];
                let result = if chunk_alg == "file" {
                    ingest_whole(path, cas, hardlink)
                        .map(|(digest, size)| {
                            vec![Chunk {
                                digest,
                                size,
                                file: name.clone(),
                                offset: 0,
                                length: size,
                            }]
                        })
                        .map_err(|error| error.to_string())
                } else {
                    ingest_cdc(
                        path,
                        name,
                        cas,
                        min_bytes,
                        avg_bytes,
                        max_bytes,
                    )
                    .map_err(|error| error.to_string())
                };
                let _ = sender.send((index, result));
            });
        }
    });
    drop(sender);
    let mut results: Vec<Vec<Chunk>> = (0..entries.len()).map(|_| Vec::new()).collect();
    for (index, result) in receiver {
        results[index] = result.map_err(|error| {
            io::Error::new(io::ErrorKind::Other, error)
        })?;
    }
    let mut chunks = results.into_iter().flatten().collect::<Vec<_>>();
    chunks.sort_by(|a, b| {
        (&a.file, a.offset, a.length, &a.digest).cmp(&(
            &b.file,
            b.offset,
            b.length,
            &b.digest,
        ))
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
            .any(|part| part.as_os_str().to_string_lossy().starts_with(".shardferry"))
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

fn ingest_whole(
    source: &Path,
    cas: &Path,
    hardlink: bool,
) -> Result<(String, u64), Box<dyn std::error::Error>> {
    let size = fs::metadata(source)?.len();
    let mut hasher = Sha256::new();
    let mut reader = BufReader::with_capacity(8 * 1024 * 1024, File::open(source)?);
    let mut buffer = vec![0u8; 8 * 1024 * 1024];
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

fn ingest_cdc(
    source: &Path,
    name: &str,
    cas: &Path,
    min_bytes: usize,
    avg_bytes: usize,
    max_bytes: usize,
) -> Result<Vec<Chunk>, Box<dyn std::error::Error>> {
    let mask_bits = (usize::BITS - (avg_bytes as u64).leading_zeros() - 1).max(1);
    let mask = if mask_bits >= 64 {
        u64::MAX
    } else {
        (1u64 << mask_bits) - 1
    };
    let mut reader = BufReader::with_capacity(8 * 1024 * 1024, File::open(source)?);
    let mut buffer = vec![0u8; 8 * 1024 * 1024];
    let mut chunk = Vec::with_capacity(max_bytes);
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
            rolling = (rolling << 1).wrapping_add(gear(byte));
            let length = chunk.len();
            if (length >= min_bytes && (rolling & mask) == 0) || length >= max_bytes {
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

fn store_chunk(
    cas: &Path,
    data: &[u8],
) -> Result<(String, u64), Box<dyn std::error::Error>> {
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

fn gear(byte: u8) -> u64 {
    let mut value = (byte as u64).wrapping_add(0x9e3779b97f4a7c15);
    value ^= value >> 30;
    value = value.wrapping_mul(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value = value.wrapping_mul(0x94d049bb133111eb);
    value ^ (value >> 31)
}
