//! SHA-256 and Merkle folding kernels.
//!
//! These back the checkpoint content digests, the source-shard checksums and
//! the migration proof roots. The digest strings are part of the on-disk and
//! on-wire contract (`sha256:<hex>`), and the Merkle fold — including the
//! domain-separation prefixes and the "duplicate the last node on odd levels"
//! rule — is reproduced exactly from the retired Python implementation.

use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::Path;

const FILE_READ_BYTES: usize = 1024 * 1024;

/// `sha256:<hex>` for an in-memory buffer.
pub fn digest_bytes(data: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(data))
}

/// Alias kept for the storage backends, which hash payloads they already hold.
pub fn sha256_bytes(data: &[u8]) -> String {
    digest_bytes(data)
}

/// Hash a sequence of buffers as if they were concatenated.
///
/// Mirrors the incremental `hashlib.sha256().update(...)` loop used by the
/// storage backends when they stream a range in chunks.
pub fn sha256_concat(chunks: &[&[u8]]) -> String {
    let mut hasher = Sha256::new();
    for chunk in chunks {
        hasher.update(chunk);
    }
    format!("sha256:{:x}", hasher.finalize())
}

/// Stream-hash a file, reading it in 1 MiB blocks.
pub fn sha256_file(path: &Path) -> Result<String, io::Error> {
    let mut hasher = Sha256::new();
    let mut reader = BufReader::with_capacity(FILE_READ_BYTES, File::open(path)?);
    let mut buffer = vec![0u8; FILE_READ_BYTES];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

/// Domain-separation prefixes of the Merkle tree.
const LEAF_PREFIX: &[u8] = b"aevrel:leaf\0";
const NODE_PREFIX: &[u8] = b"aevrel:node\0";
const EMPTY_PAYLOAD: &[u8] = b"aevrel:empty";

/// Fold `sha256:<hex>` digests into a single Merkle root.
///
/// Leaves are sorted lexicographically first, hashed with a leaf prefix, and
/// then pairwise folded with a node prefix; an odd node is paired with itself.
/// An empty digest list hashes the `aevrel:empty` sentinel.
pub fn merkle_root(digests: &[&str]) -> Result<String, String> {
    if digests.is_empty() {
        return Ok(digest_bytes(EMPTY_PAYLOAD));
    }
    let mut sorted: Vec<&str> = digests.to_vec();
    sorted.sort_unstable();
    let mut level: Vec<[u8; 32]> = Vec::with_capacity(sorted.len());
    for digest in sorted {
        let raw = decode_digest(digest)?;
        let mut hasher = Sha256::new();
        hasher.update(LEAF_PREFIX);
        hasher.update(raw);
        level.push(hasher.finalize().into());
    }
    while level.len() > 1 {
        let mut next_level = Vec::with_capacity(level.len().div_ceil(2));
        for index in (0..level.len()).step_by(2) {
            let left = level[index];
            let right = level.get(index + 1).copied().unwrap_or(left);
            let mut hasher = Sha256::new();
            hasher.update(NODE_PREFIX);
            hasher.update(left);
            hasher.update(right);
            next_level.push(hasher.finalize().into());
        }
        level = next_level;
    }
    Ok(format!("sha256:{}", hex_encode(&level[0])))
}

/// Decode the hex body of a `sha256:<hex>` digest.
fn decode_digest(digest: &str) -> Result<[u8; 32], String> {
    let body = digest
        .split_once(':')
        .map(|(_, body)| body)
        .ok_or_else(|| format!("invalid digest: {digest}"))?;
    let raw = hex_decode(body).ok_or_else(|| format!("invalid digest: {digest}"))?;
    raw.try_into()
        .map_err(|_| format!("digest is not 32 bytes: {digest}"))
}

/// Lowercase hex decoding; returns `None` on any non-hex character.
fn hex_decode(value: &str) -> Option<Vec<u8>> {
    if !value.len().is_multiple_of(2) {
        return None;
    }
    let mut out = Vec::with_capacity(value.len() / 2);
    let bytes = value.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let high = hex_digit(bytes[index])?;
        let low = hex_digit(bytes[index + 1])?;
        out.push((high << 4) | low);
        index += 2;
    }
    Some(out)
}

fn hex_digit(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Lowercase hex encoding without pulling in a formatting dependency.
fn hex_encode(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_bytes_matches_known_vectors() {
        assert_eq!(
            digest_bytes(b""),
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            digest_bytes(b"aevrel:empty"),
            "sha256:50733009307988f4946c9e6115d2d6435779c65ee27e9d97a04c5aa9e1e5df3f"
        );
        assert_eq!(
            digest_bytes(b"aevrel:leaf\0"),
            "sha256:2e1a5524f3c58f9fe9f582066070dd8dd2ff9966666aa433ba1762c237899112"
        );
    }

    #[test]
    fn hex_round_trips() {
        assert_eq!(hex_decode("00ff10").unwrap(), vec![0x00, 0xff, 0x10]);
        assert!(hex_decode("0").is_none());
        assert!(hex_decode("zz").is_none());
        assert_eq!(hex_encode(&[0x00, 0xff, 0x10]), "00ff10");
    }

    #[test]
    fn sha256_concat_matches_whole_buffer() {
        let data = b"the quick brown fox".to_vec();
        let split = data.split_at(5);
        assert_eq!(sha256_concat(&[split.0, split.1]), sha256_bytes(&data));
    }

    #[test]
    fn merkle_root_of_empty_list_is_the_sentinel() {
        assert_eq!(merkle_root(&[]).unwrap(), digest_bytes(b"aevrel:empty"));
    }

    #[test]
    fn merkle_root_is_order_independent() {
        let a = digest_bytes(b"a");
        let b = digest_bytes(b"b");
        assert_eq!(
            merkle_root(&[&a, &b]).unwrap(),
            merkle_root(&[&b, &a]).unwrap()
        );
    }

    #[test]
    fn merkle_root_rejects_malformed_digests() {
        assert!(merkle_root(&["deadbeef"]).is_err());
        assert!(merkle_root(&["sha256:zz"]).is_err());
    }
}
