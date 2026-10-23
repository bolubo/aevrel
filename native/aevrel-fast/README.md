# aevrel-fast

Rust file-level CAS ingest for the aevrel CDP data path.

It streams SHA-256 over each target file, writes the content-addressed object,
and prefers a hardlink for immutable target files. The Python layer remains the
source of truth for manifest canonicalization; the binary only returns chunk
descriptors.

Build:

```bash
cd native/aevrel-fast
cargo build --release
```

Python uses `native/aevrel-fast/target/release/aevrel-fast` when present,
or the path in `AEVREL_FAST`. If unavailable, it falls back to the Python
ingest path.

Direct invocation:

```bash
aevrel-fast --root /checkpoints/version --cas /target/cas
aevrel-fast --file-map files.json --cas /target/cas
```
