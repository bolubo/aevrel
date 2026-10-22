# shardferry-fast

Rust file-level CAS ingest for the shardferry CDP data path.

It streams SHA-256 over each target file, writes the content-addressed object,
and prefers a hardlink for immutable target files. The Python layer remains the
source of truth for manifest canonicalization; the binary only returns chunk
descriptors.

Build:

```bash
cd native/shardferry-fast
cargo build --release
```

Python uses `native/shardferry-fast/target/release/shardferry-fast` when present,
or the path in `SHARDFERRY_FAST`. If unavailable, it falls back to the Python
ingest path.

Direct invocation:

```bash
shardferry-fast --root /checkpoints/version --cas /target/cas
shardferry-fast --file-map files.json --cas /target/cas
```
