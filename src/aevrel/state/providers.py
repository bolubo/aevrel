"""Controlled providers for rank-local resume state."""

from __future__ import annotations

import json
import random
from typing import Any, Callable, Protocol


class StateProviderError(ValueError):
    """Raised when a provider cannot capture or restore state safely."""


class StateProvider(Protocol):
    name: str
    format: str

    def capture(self) -> bytes:
        ...

    def restore(self, data: bytes) -> None:
        ...


class PythonRandomProvider:
    name = "python.random"
    format = "python-random-json-v1"

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random

    def capture(self) -> bytes:
        version, keys, gauss = self.rng.getstate()
        return json.dumps(
            {"version": version, "keys": list(keys), "gauss": gauss},
            separators=(",", ":"),
        ).encode("utf-8")

    def restore(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
            version = int(payload["version"])
            keys = tuple(int(value) for value in payload["keys"])
            gauss = payload.get("gauss")
            self.rng.setstate((version, keys, gauss))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateProviderError("invalid Python RNG state") from exc


class NumpyRandomProvider:
    name = "numpy.random"
    format = "numpy-random-json-v1"

    def __init__(self, rng: Any | None = None) -> None:
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StateProviderError("numpy is required for NumpyRandomProvider") from exc
        self.np = np
        self.rng = rng or np.random

    def capture(self) -> bytes:
        value = self.rng.get_state()
        return json.dumps(
            {
                "name": value[0],
                "array": value[1].tolist(),
                "pos": int(value[2]),
                "has_gauss": int(value[3]),
                "cached_gaussian": float(value[4]),
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def restore(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
            self.rng.set_state(
                (
                    str(payload["name"]),
                    self.np.asarray(payload["array"], dtype=self.np.uint32),
                    int(payload["pos"]),
                    int(payload["has_gauss"]),
                    float(payload["cached_gaussian"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateProviderError("invalid NumPy RNG state") from exc


class TorchRNGProvider:
    format = "torch-rng-bytes-v1"

    def __init__(self, device: str = "cpu") -> None:
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StateProviderError("torch is required for TorchRNGProvider") from exc
        self.torch = torch
        self.device = device
        self.name = (
            "torch.cuda.rng"
            if device.startswith("cuda")
            else "torch.cpu.rng"
        )

    def capture(self) -> bytes:
        if self.device.startswith("cuda"):
            return self.torch.cuda.get_rng_state(self.device).cpu().numpy().tobytes()
        return self.torch.get_rng_state().numpy().tobytes()

    def restore(self, data: bytes) -> None:
        try:
            state = self.torch.frombuffer(
                bytearray(data),
                dtype=self.torch.uint8,
            ).clone()
            if self.device.startswith("cuda"):
                self.torch.cuda.set_rng_state(state, self.device)
            else:
                self.torch.set_rng_state(state)
        except Exception as exc:
            raise StateProviderError("invalid Torch RNG state") from exc


class JsonStateProvider:
    """Small explicit adapter for JSON-safe state dicts.

    This is for scheduler/dataloader metadata, not arbitrary Python objects.
    """

    def __init__(
        self,
        name: str,
        capture: Callable[[], Any],
        restore: Callable[[Any], None],
        *,
        format: str = "json-v1",
    ) -> None:
        if not name or not format:
            raise ValueError("state provider name and format are required")
        self.name = name
        self.format = format
        self._capture = capture
        self._restore = restore

    def capture(self) -> bytes:
        try:
            return json.dumps(
                self._capture(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StateProviderError(
                f"state provider {self.name} is not JSON-safe"
            ) from exc

    def restore(self, data: bytes) -> None:
        try:
            self._restore(json.loads(data))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateProviderError(
                f"state provider {self.name} could not restore {self.format}"
            ) from exc


def builtin_providers(names: list[str]) -> list[StateProvider]:
    """Construct only the explicitly requested built-in providers."""
    providers: list[StateProvider] = []
    for name in names:
        normalized = name.strip().lower()
        if normalized == "python.random":
            providers.append(PythonRandomProvider())
        elif normalized == "numpy.random":
            providers.append(NumpyRandomProvider())
        elif normalized == "torch.cpu.rng":
            providers.append(TorchRNGProvider("cpu"))
        elif normalized == "torch.cuda.rng":
            providers.append(TorchRNGProvider("cuda"))
        else:
            raise StateProviderError(f"unknown state provider: {name}")
    if not providers:
        raise StateProviderError("at least one state provider is required")
    return providers
