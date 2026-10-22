# CheckpointCutover TLA+

This is a small protocol model for the cutover invariants:

- only the current fence owner can promote;
- fence epochs never move backwards;
- `current` is either absent or exactly one version;
- promotion only points at the fenced version;
- a paused source requires a verified manifest.

The model intentionally treats crash as a stuttering step; durability is
represented by the fact that fence and current are not both rewritten
atomically. The implementation provides that sequencing with target-root
locking and atomic current-pointer publication.

TLC 2.19 was run with the finite config and completed without an invariant
violation:

```text
455 states generated, 91 distinct states found, 0 states left on queue.
The depth of the complete state graph search is 8.
```

If TLC is available:

```text
java -cp tla2tools.jar tlc2.TLC CheckpointCutover.tla \
  -config CheckpointCutover.cfg
```

The config fixes a finite exploration to two versions and two controller
owners. If TLC is not installed, the module is still checked by review and by
the Python crash/state tests that implement the same transitions.
