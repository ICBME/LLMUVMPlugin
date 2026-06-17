from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .corpus import FuzzCase


@dataclass(frozen=True)
class ReplayResult:
    actual: Any
    expected: Any | None = None
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class TargetDriver(Protocol):
    async def reset(self) -> None:
        ...

    async def execute(self, case: FuzzCase) -> ReplayResult:
        ...
