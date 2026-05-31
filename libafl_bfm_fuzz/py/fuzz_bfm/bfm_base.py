from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .corpus import FuzzCase


@dataclass(frozen=True)
class ReplayResult:
    actual: str
    expected: str | None = None
    detail: str = ""


class TargetDriver(Protocol):
    async def reset(self) -> None:
        ...

    async def execute(self, case: FuzzCase) -> ReplayResult:
        ...
