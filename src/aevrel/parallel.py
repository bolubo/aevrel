"""Bounded parallelism for independent byte-range work.

Verification decomposes into tasks that share no mutable state: one source
shard to hash, one target node to compare. Those are the only structures in
the codebase that run work concurrently, and the reason they can is that the
native kernels release the GIL for the whole scan -- a kernel that holds it
turns the pool into a slower sequential loop, which is exactly the failure we
shipped in the first cut of `sha256_file` and have to keep out.

Concurrency is offered for local sources only. Remote sources share one HTTP
client and a rate limiter, and a checkpoint read from object storage is
bandwidth-bound long before it is core-bound, so extra threads would buy
nothing and would interleave requests that were never written to be
concurrent.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlsplit

LOCAL_SCHEMES = frozenset({"file", "dcpzip"})

# Beyond this the cores stop paying for themselves: the page cache and the
# memory bus saturate (16 worker streams measured at ~9x on a 24-core box),
# and a control plane that eats every core on a node also slows down whatever
# else is running there. Raise the cap with `AEVREL_VERIFY_WORKERS` when the
# workload is large and the box is otherwise idle.
MAX_WORKERS = 16


def worker_count(tasks: int, uris: Iterable[str]) -> int:
    """Number of threads worth spawning for `tasks` byte-range jobs.

    Returns `1` when there is nothing to gain: fewer than two tasks, or any
    source that is not local. The caller is then expected to run sequentially,
    matching the behaviour this module replaced.
    """
    if tasks < 2:
        return 1
    for uri in uris:
        if urlsplit(uri).scheme not in LOCAL_SCHEMES:
            return 1
    override = os.environ.get("AEVREL_VERIFY_WORKERS", "").strip()
    if override:
        try:
            return max(1, min(tasks, int(override)))
        except ValueError:
            pass
    return max(1, min(tasks, os.cpu_count() or 1, MAX_WORKERS))
