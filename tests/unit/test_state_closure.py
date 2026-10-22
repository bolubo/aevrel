from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from shardferry.state import (
    JsonStateProvider,
    NumpyRandomProvider,
    PythonRandomProvider,
    StateBundle,
    StateBundleError,
    TorchRNGProvider,
    capture_state,
    restore_state,
    verify_state,
)


def test_python_rng_provider_roundtrip() -> None:
    source = random.Random(1234)
    provider = PythonRandomProvider(source)
    captured = provider.capture()
    expected = source.random()

    target = random.Random(999)
    PythonRandomProvider(target).restore(captured)

    assert target.random() == expected


def test_numpy_rng_provider_roundtrip() -> None:
    np = pytest.importorskip("numpy")
    source = np.random.RandomState(1234)
    provider = NumpyRandomProvider(source)
    captured = provider.capture()
    expected = source.random_sample()

    target = np.random.RandomState(999)
    NumpyRandomProvider(target).restore(captured)

    assert target.random_sample() == expected


def test_json_state_provider_roundtrip() -> None:
    value = {"step": 10, "scheduler": {"last_epoch": 3}}
    provider = JsonStateProvider(
        "scheduler",
        lambda: value,
        lambda payload: setattr(provider, "restored", payload),
    )

    provider.restore(provider.capture())

    assert provider.restored == value


def test_rank_state_bundle_preserves_distinct_rank_values(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    expected: dict[int, float] = {}
    for rank in range(4):
        rng = random.Random(1234 + rank)
        capture_state(
            root=root,
            checkpoint_id="job-1",
            version="step-100",
            rank=rank,
            source_world_size=4,
            providers=[PythonRandomProvider(rng)],
        )
        expected[rank] = rng.random()

    bundle = StateBundle.read(root)
    verify_state(bundle, require_complete=True)
    assert bundle.manifest.complete

    for rank in range(4):
        restored = random.Random(9999)
        restore_state(
            bundle,
            target_rank=rank,
            providers=[PythonRandomProvider(restored)],
            strict=True,
        )
        assert restored.random() == expected[rank]


def test_rank_state_bundle_detects_missing_rank(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=2,
        providers=[PythonRandomProvider(random.Random(1))],
    )

    bundle = StateBundle.read(root)
    assert not bundle.manifest.complete
    with pytest.raises(StateBundleError, match="not closure-complete"):
        verify_state(bundle, require_complete=True)


def test_rank_state_bundle_detects_corrupt_blob(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=1,
        providers=[PythonRandomProvider(random.Random(1))],
    )
    bundle = StateBundle.read(root)
    digest = bundle.manifest.entries[0].digest
    StateBundle.blob_path(root, digest).write_bytes(b"corrupted")

    with pytest.raises(StateBundleError, match="blob mismatch"):
        StateBundle.read(root)


def test_torch_cpu_rng_provider_roundtrip() -> None:
    torch = pytest.importorskip("torch")
    torch.manual_seed(1234)
    source = TorchRNGProvider("cpu")
    captured = source.capture()
    expected = torch.rand(4)

    torch.manual_seed(9999)
    TorchRNGProvider("cpu").restore(captured)
    actual = torch.rand(4)

    assert torch.equal(actual, expected)


def test_torch_cuda_rng_provider_roundtrip() -> None:
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    torch.manual_seed(1234)
    source = TorchRNGProvider("cuda")
    captured = source.capture()
    expected = torch.rand(4, device="cuda")

    torch.manual_seed(9999)
    TorchRNGProvider("cuda").restore(captured)
    actual = torch.rand(4, device="cuda")

    assert torch.equal(actual, expected)


def test_bundle_manifest_has_json_safe_metadata(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=1,
        providers=[
            JsonStateProvider(
                "scheduler",
                lambda: {"last_epoch": 4},
                lambda _: None,
            )
        ],
    )

    payload = json.loads(
        StateBundle.manifest_path(root).read_text(encoding="utf-8")
    )
    assert payload["entries"][0]["name"] == "scheduler"
    assert payload["root"] == StateBundle.read(root).manifest.root


def test_state_cli_capture_and_verify(tmp_path: Path) -> None:
    from shardferry.cli import main

    root = tmp_path / "state"
    assert main(
        [
            "state",
            "capture",
            "--root",
            str(root),
            "--checkpoint-id",
            "job-1",
            "--checkpoint-version",
            "step-100",
            "--rank",
            "0",
            "--source-world",
            "1",
            "--providers",
            "python.random",
        ]
    ) == 0
    assert main(
        [
            "state",
            "verify",
            "--root",
            str(root),
            "--require-complete",
        ]
    ) == 0
