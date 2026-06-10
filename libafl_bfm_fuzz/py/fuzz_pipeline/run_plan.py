from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


RunResults = dict[str, object]
RunStageHandler = Callable[[RunResults], object]


@dataclass(frozen=True)
class RunStage:
    """A named orchestration stage in a run-level plan."""

    name: str
    handler: RunStageHandler
    result_key: str | None = None
    merge_mapping: bool = False
    store_none: bool = False


@dataclass(frozen=True)
class RunPlan:
    """Ordered run-level plan built from connector-wrapped stage handlers."""

    name: str
    stages: tuple[RunStage, ...]
    write_topology: bool = True


class RunPlanExecutor:
    def __init__(self, *, write_topology: Callable[[], None] | None = None):
        self.write_topology = write_topology

    def run(
        self,
        plan: RunPlan,
        *,
        initial_results: RunResults | None = None,
    ) -> RunResults:
        if plan.write_topology and self.write_topology is not None:
            self.write_topology()
        results: RunResults = dict(initial_results or {})
        for stage in plan.stages:
            value = stage.handler(results)
            self._record_stage_result(results, stage, value)
        return results

    def _record_stage_result(
        self,
        results: RunResults,
        stage: RunStage,
        value: object,
    ) -> None:
        if stage.merge_mapping:
            if not isinstance(value, dict):
                raise TypeError(
                    f"run plan stage {stage.name!r} must return a mapping to merge"
                )
            results.update({str(key): item for key, item in value.items()})
            return
        key = stage.result_key or stage.name
        if value is not None or stage.store_none:
            results[key] = value
