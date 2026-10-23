"""Local provider contracts used by the Python runtime adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import CheckpointRecord


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    version: str
    discover: bool = True
    inventory: bool = True
    read: bool = True
    load: bool = False
    drill: bool = False
    async_save: bool = False
    reshard: bool = False
    state_closure: bool = False
    live: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "discover": self.discover,
            "inventory": self.inventory,
            "read": self.read,
            "load": self.load,
            "drill": self.drill,
            "async_save": self.async_save,
            "reshard": self.reshard,
            "state_closure": self.state_closure,
            "live": self.live,
        }


class StateProvider(Protocol):
    """Read-only inventory contract. It must never modify the checkpoint."""

    name: str

    def supports(self, checkpoint: str | Path) -> bool:
        ...

    def inventory(
        self,
        checkpoint: str | Path,
        *,
        namespace: str = "",
        job_uid: str = "",
        version: str = "",
        step: int | None = None,
    ) -> CheckpointRecord:
        ...

    def capabilities(self) -> ProviderCapabilities:
        ...
