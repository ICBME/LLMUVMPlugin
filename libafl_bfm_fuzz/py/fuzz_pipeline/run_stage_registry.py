from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Callable

from .run_plan import RunStage


RunStageFactory = Callable[[], RunStage]


@dataclass(frozen=True)
class RegisteredRunStage:
    name: str
    factory: RunStageFactory


class RunStageRegistry:
    """Registry that turns stage names into concrete run-plan stages."""

    def __init__(
        self,
        stages: Iterable[RegisteredRunStage] | Mapping[str, RunStageFactory] = (),
    ):
        self._factories: dict[str, RunStageFactory] = {}
        if isinstance(stages, Mapping):
            for name, factory in stages.items():
                self.register(name, factory)
        else:
            for stage in stages:
                self.register(stage.name, stage.factory)

    def register(
        self,
        name: str,
        factory: RunStageFactory,
        *,
        overwrite: bool = False,
    ) -> None:
        if name in self._factories and not overwrite:
            raise ValueError(f"run stage {name!r} is already registered")
        self._factories[name] = factory

    def build(self, name: str) -> RunStage:
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise ValueError(f"unknown run stage: {name}") from exc
        stage = factory()
        if stage.name != name:
            raise ValueError(
                f"run stage factory {name!r} returned stage {stage.name!r}"
            )
        return stage

    def build_many(self, names: Iterable[str]) -> tuple[RunStage, ...]:
        return tuple(self.build(name) for name in names)

    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)
