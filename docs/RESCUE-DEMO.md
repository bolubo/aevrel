# Rescue Demo

The rescue demo shows how an interrupted local migration resumes from its
journal instead of starting over.

```bash
python scripts/rescue_demo.py --work-dir ./aevrel-rescue
```

Expected outputs:

- `rescue-proof.json`: journal state, rerun duration, source digest, and target
  load result;
- `timeline.html`: a visual timeline of interruption, resume, and commit.

## What it demonstrates

1. An in-progress migration leaves resumable journal and staging state.
2. Rerunning the same command skips completed nodes.
3. The remaining data is transferred, verified, and committed.
4. The target DCP loads successfully.
5. The source digest is unchanged.

This is a deterministic local interruption test. It does not simulate
controller restart, target-agent death, network partition, or cross-node
failure. Those cases belong in the kind/E2E fault matrix.

## Validation

```bash
python -m pytest -q tests/integration/test_rescue_demo.py
```
