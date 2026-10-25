"""The agent's write boundary, as a machine-checked property.

`agent/server.py` is defined as read-only on the source plus target-side
verification. Three write channels exist, and all three are accounted for:

* six direct filesystem calls, all inside two methods — `cleanup_target`
  (guarded to the configured target root) and `install_metadata` (into the
  agent's own CAS store);
* task state through `TaskStore`, rooted at the configured `state_dir`;
* whatever the injected `executor` and `committer` callbacks do, which is the
  caller's contract rather than this module's.

None of them can reach the source checkpoint. That claim is only worth
something if it is enforced, so this module pins it two ways:

* structurally, by walking the module's AST and requiring every direct
  filesystem mutation to live in one of the two allowed methods — with a
  self-test proving the detector actually fires and does not fire on
  lookalikes such as `str.replace` or `wfile.write`;
* behaviourally, by exercising every read path and the whole task lifecycle and
  proving the source tree is byte-identical afterwards, and that the allowed
  writers stay inside the store root and the configured target root.
"""

from __future__ import annotations

import ast
import hashlib
import threading
from pathlib import Path

import pytest
import yaml

from aevrel.agent.server import AgentApp, AgentError, build_agent_server
from aevrel.planning import plan as build_plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.unit.test_agent import _local_ip, _post, _task

SERVER = Path(__file__).resolve().parents[2] / "src" / "aevrel" / "agent" / "server.py"

# Mutators reached through a module whose name makes the target unambiguous.
MODULE_MUTATORS = {
    "io": {"open"},
    "os": {
        "chmod",
        "chown",
        "fdopen",
        "link",
        "makedirs",
        "mkdir",
        "remove",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "rmdir",
        "symlink",
        "truncate",
        "unlink",
        "utime",
        "write",
    },
    "shutil": {"chmod", "copy", "copy2", "copyfile", "copytree", "move", "rmtree"},
    "tempfile": {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryDirectory"},
}

# `pathlib` methods that only exist for mutation, so the receiver's type does
# not need to be inferred. Ambiguous names such as `replace` (also `str.replace`)
# and `remove` (also `list.remove`) are deliberately absent: those are caught
# through their module instead.
PATH_MUTATORS = {
    "chmod",
    "hardlink_to",
    "mkdir",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}

# Characters whose presence in an `open` mode means "mutates the filesystem".
WRITE_MODE_CHARS = set("wax+")

# The only two methods allowed to mutate anything. `cleanup_target` is confined
# to the configured target root and `install_metadata` to the agent's own CAS
# store; neither can reach the source checkpoint.
ALLOWED_WRITERS = {"cleanup_target", "install_metadata"}


def _is_write_mode(call: ast.Call) -> bool:
    """Whether an `open`-style call opens for writing."""
    mode: ast.expr | None = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        # A computed mode cannot be proven read-only, so treat it as a write.
        return mode is not None
    return bool(WRITE_MODE_CHARS & set(mode.value))


def _mutation_name(call: ast.Call) -> str | None:
    """Name of the filesystem mutation `call` performs, if any."""
    function = call.func
    if not isinstance(function, ast.Attribute):
        if isinstance(function, ast.Name) and function.id == "open" and _is_write_mode(call):
            return "open(write)"
        return None
    if isinstance(function.value, ast.Name):
        allowed = MODULE_MUTATORS.get(function.value.id)
        if allowed is not None and function.attr in allowed:
            if function.attr == "open" and not _is_write_mode(call):
                return None
            return f"{function.value.id}.{function.attr}"
    if function.attr == "open" and _is_write_mode(call):
        return "open(write)"
    if function.attr in PATH_MUTATORS:
        return function.attr
    return None


def mutations(source: str) -> dict[str, set[str]]:
    """Map each function in `source` to the filesystem mutations it makes."""
    tree = ast.parse(source)
    found: dict[str, set[str]] = {}

    def walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                name = _mutation_name(child)
                if name is not None:
                    found.setdefault(owner, set()).add(name)
            walk(child, owner)

    walk(tree, "<module>")
    return found


def server_mutations() -> dict[str, set[str]]:
    return mutations(SERVER.read_text(encoding="utf-8"))


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Content plus size for every file under `root`."""
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            state[str(path.relative_to(root))] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return state


@pytest.fixture()
def source_tree(tmp_path):
    """A real sharded source checkpoint plus its spec file."""
    spec, arrays, _ = generate_sharded_spec(
        (8, 4), (2, 2), ("Shard(0)", "Shard(1)"), (3, 1), ("Shard(0)", "Replicate()")
    )
    materialize_source_files(spec, arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return spec, spec_path, tmp_path / "source"


def test_only_the_two_allowed_methods_mutate_the_filesystem() -> None:
    """No other function may reach a filesystem mutator.

    This is the gate that keeps "read-only agent" true under future edits: a
    new `write_bytes` anywhere else in the module fails here by name.
    """
    offenders = {
        owner: sorted(calls)
        for owner, calls in server_mutations().items()
        if owner not in ALLOWED_WRITERS
    }
    assert offenders == {}, f"unexpected filesystem mutation: {offenders}"


def test_the_allowed_writers_are_exactly_the_two_we_expect() -> None:
    assert set(server_mutations()) == ALLOWED_WRITERS


def test_the_detector_fires_on_a_mutation_in_an_unlisted_function() -> None:
    """A gate that can never trigger is worse than no gate."""
    source = """
import os
from pathlib import Path

def read_only():
    Path("x").read_bytes()

def sneaky():
    Path("x").write_bytes(b"payload")

def also_sneaky():
    with open("x", "w") as stream:
        stream.write("payload")
"""
    detected = mutations(source)
    assert set(detected) == {"sneaky", "also_sneaky"}


def test_the_detector_ignores_harmless_lookalikes() -> None:
    """`str.replace`, `list.remove`, `wfile.write` and read-only opens must pass."""
    source = """
def read_only(response):
    text = "a-b".replace("-", "")
    items = ["a", "b"]
    items.remove("a")
    response.wfile.write(b"body")
    with open("x") as stream:
        stream.read()
    with open("y", "rb") as stream:
        stream.read()
    return text, items
"""
    assert mutations(source) == {}


def test_read_paths_leave_the_source_tree_untouched(source_tree, tmp_path) -> None:
    spec, spec_path, source_root = source_tree
    app = AgentApp(
        controller_token="secret",
        state_dir=tmp_path / "state",
        spec_path=spec_path,
        target_root=tmp_path / "target",
    )
    before = snapshot(source_root)

    app.capabilities()
    for rank in app.ranks()["ranks"]:
        app.inventory(rank)
    app.plan(spec)

    assert snapshot(source_root) == before


def test_task_lifecycle_leaves_the_source_tree_untouched(
    source_tree, tmp_path
) -> None:
    """The task path is the only mutating HTTP surface; prove it stays off source."""
    _, spec_path, source_root = source_tree
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "state",
        executor=lambda task: None,
        committer=lambda task: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        before = snapshot(source_root)
        task = _post(base, "/v1/tasks", _task(tmp_path))
        for action in ("prepare", "execute", "commit"):
            task = _post(base, f"/v1/tasks/{task['task_id']}/{action}", {})
        assert task["state"] == "committed"
        assert snapshot(source_root) == before
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_plan_is_a_pass_through_not_a_decision(source_tree) -> None:
    """The agent wraps the planner; it does not decide anything itself."""
    spec, spec_path, _ = source_tree
    app = AgentApp(
        controller_token="secret",
        state_dir=spec_path.parent / "state",
        spec_path=spec_path,
        target_root=spec_path.parent / "target",
    )
    response = app.plan(spec)
    assert response["plan"] == build_plan(spec)
    assert app.plan(spec) == response, "plan must be deterministic"


def test_preflight_is_a_dry_check(source_tree, tmp_path) -> None:
    _, spec_path, _ = source_tree
    target_root = tmp_path / "target"
    app = AgentApp(
        controller_token="secret",
        state_dir=tmp_path / "state",
        spec_path=spec_path,
        target_root=target_root,
    )
    destination = target_root / "rank0"
    result = app.preflight(destination)
    assert result["ok"] is True
    assert result["nonempty"] is False
    assert not destination.exists(), "preflight must not create anything"


def test_cleanup_target_stays_inside_the_configured_root(source_tree, tmp_path) -> None:
    _, spec_path, _ = source_tree
    target_root = tmp_path / "target"
    app = AgentApp(
        controller_token="secret",
        state_dir=tmp_path / "state",
        spec_path=spec_path,
        target_root=target_root,
    )
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    marker = outside / "keep.bin"
    marker.write_bytes(b"do not delete me")

    with pytest.raises(AgentError, match="outside"):
        app.cleanup_target(outside)
    assert marker.read_bytes() == b"do not delete me"

    target_root.mkdir(exist_ok=True)
    (target_root / "current").mkdir()
    with pytest.raises(AgentError, match="refusing to remove"):
        app.cleanup_target(target_root)
    assert target_root.is_dir()


def test_installed_metadata_never_lands_in_the_checkpoint(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    (checkpoint / ".metadata").write_bytes(b"original")
    app = AgentApp(
        controller_token="secret",
        state_dir=tmp_path / "state",
        dcp_checkpoint=checkpoint,
        target_root=tmp_path / "target",
    )
    before = snapshot(checkpoint)

    result = app.install_metadata(b"peer metadata")

    assert result["ok"] is True
    assert result["metadata_hash"] == "sha256:" + hashlib.sha256(
        b"peer metadata"
    ).hexdigest()
    assert snapshot(checkpoint) == before, "the checkpoint must stay untouched"
    installed = app.store.root / "metadata" / result["metadata_hash"].removeprefix(
        "sha256:"
    ) / ".metadata"
    assert installed.read_bytes() == b"peer metadata"
