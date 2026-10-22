from __future__ import annotations

from collections import deque


VERSIONS = ("step-100", "step-200")
OWNERS = ("controller-a", "controller-b")


def initial():
    return {
        "current": None,
        "phase": "following",
        "fence_epoch": 0,
        "fence_owner": None,
        "fence_version": None,
        "manifest_ready": False,
        "paused": False,
    }


def successors(state):
    result = []
    for owner in OWNERS:
        for epoch in (1, 2, 3):
            if epoch < state["fence_epoch"]:
                continue
            if epoch == state["fence_epoch"] and owner != state["fence_owner"]:
                continue
            for version in VERSIONS:
                item = dict(state)
                item.update(
                    fence_epoch=epoch,
                    fence_owner=owner,
                    fence_version=version,
                )
                result.append(item)
    if state["phase"] == "following" and state["manifest_ready"]:
        item = dict(state)
        item.update(phase="paused", paused=True)
        result.append(item)
    if (
        state["phase"] == "paused"
        and state["manifest_ready"]
        and state["fence_version"] is not None
    ):
        item = dict(state)
        item.update(
            current=state["fence_version"],
            phase="promoted",
            paused=False,
        )
        result.append(item)
    if state["phase"] == "paused":
        item = dict(state)
        item.update(phase="following", paused=False)
        result.append(item)
    return result


def check(state):
    assert state["current"] is None or state["current"] in VERSIONS
    assert state["fence_epoch"] >= 0
    if state["phase"] == "promoted":
        assert state["current"] == state["fence_version"]
    if state["phase"] == "paused":
        assert state["manifest_ready"]


def test_cutover_model_invariants_hold_for_bounded_exploration() -> None:
    queue = deque([(initial(), 0)])
    seen = set()
    while queue:
        state, depth = queue.popleft()
        key = repr(sorted(state.items()))
        if key in seen:
            continue
        seen.add(key)
        check(state)
        if depth == 3:
            continue
        for child in successors(state):
            queue.append((child, depth + 1))
    assert seen
