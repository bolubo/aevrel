# Experiments

Benchmarks, probes, GPU and multi-node experiments, and LRS demos that are not
part of the shipped product and are not covered by CI.

Rules:

- Nothing in CI, tests, or `scripts/` may reference this directory; a test
  enforces that. The moment an experiment becomes load-bearing, promote it:
  move it to [`scripts/`](../scripts/README.md) as repository tooling, or fold
  the real logic into `src/aevrel/` or `native/` with tests — then delete it
  from here.
- Files here may be deleted or rewritten at any time without notice.
- Treat output as evidence only when the script actually ran in the
  environment it claims, and never paste unredacted output into a public
  issue.

Each script is standalone. Run one with `--help` first; most need PyTorch, a
GPU, or a Kubernetes cluster that is not part of the standard toolchain.
