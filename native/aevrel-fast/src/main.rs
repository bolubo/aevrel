//! CLI entry point for the `aevrel-fast` chunker.
//!
//! The in-process kernels live in the library target (`lib.rs`) so the Python
//! data plane can import them as an extension module; this binary only adds
//! the command-line surface used for CDC / CAS ingest.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    aevrel_fast::cdc::main()
}
