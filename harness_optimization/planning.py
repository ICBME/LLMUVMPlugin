from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

from ConnectGraph import StepPolicy


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
    requires_results: tuple[str, ...] = ()
    produces_results: tuple[str, ...] = ()
    input_roles: tuple[str, ...] = ()
    output_roles: tuple[str, ...] = ()
    policy: StepPolicy = field(default_factory=StepPolicy)

    def result_keys(self) -> tuple[str, ...]:
        if self.produces_results:
            return self.produces_results
        if self.merge_mapping:
            return ()
        return (self.result_key or self.name,)


@dataclass(frozen=True)
class RunPlan:
    """Ordered run-level plan built from connector-wrapped stage handlers."""

    name: str
    stages: tuple[RunStage, ...]
    write_topology: bool = True
    initial_result_keys: tuple[str, ...] = ()
    initial_artifact_roles: tuple[str, ...] = ()

    def validate(
        self,
        *,
        initial_result_keys: tuple[str, ...] = (),
        initial_artifact_roles: tuple[str, ...] = (),
    ) -> None:
        result_keys = set(self.initial_result_keys) | set(initial_result_keys)
        artifact_roles = set(self.initial_artifact_roles) | set(initial_artifact_roles)
        for stage in self.stages:
            missing_results = set(stage.requires_results) - result_keys
            if missing_results:
                raise ValueError(
                    f"run plan {self.name!r} stage {stage.name!r} missing "
                    f"required result key(s): {sorted(missing_results)}"
                )
            missing_roles = set(stage.input_roles) - artifact_roles
            if missing_roles:
                raise ValueError(
                    f"run plan {self.name!r} stage {stage.name!r} missing "
                    f"required artifact role(s): {sorted(missing_roles)}"
                )
            produced_results = stage.result_keys()
            if len(set(produced_results)) != len(produced_results):
                raise ValueError(
                    f"run plan {self.name!r} stage {stage.name!r} declares "
                    "duplicate produced result key(s)"
                )
            duplicate_results = set(produced_results) & result_keys
            if duplicate_results:
                raise ValueError(
                    f"run plan {self.name!r} stage {stage.name!r} would overwrite "
                    f"result key(s): {sorted(duplicate_results)}"
                )
            result_keys.update(produced_results)
            artifact_roles.update(stage.output_roles)


class RunPlanExecutor:
    def __init__(self, *, write_topology: Callable[[], None] | None = None):
        self.write_topology = write_topology

    def run(
        self,
        plan: RunPlan,
        *,
        initial_results: RunResults | None = None,
    ) -> RunResults:
        results: RunResults = dict(initial_results or {})
        plan.validate(initial_result_keys=tuple(results))
        if plan.write_topology and self.write_topology is not None:
            self.write_topology()
        for stage in plan.stages:
            try:
                self._validate_stage_ready(results, stage)
                value = self._call_stage_handler(stage, results)
                self._record_stage_result(results, stage, value)
                self._validate_stage_outputs(results, stage)
            except Exception as exc:  # noqa: BLE001 - policy decides whether execution continues
                if stage.policy.fail_main_on_step_error:
                    raise
                self._record_stage_error(results, stage, exc)
                continue
        return results

    def _validate_stage_ready(
        self,
        results: RunResults,
        stage: RunStage,
    ) -> None:
        missing = [name for name in stage.requires_results if name not in results]
        if missing:
            raise ValueError(
                f"run plan stage {stage.name!r} missing runtime result key(s): "
                f"{sorted(missing)}"
            )

    def _validate_stage_outputs(
        self,
        results: RunResults,
        stage: RunStage,
    ) -> None:
        if not stage.produces_results:
            return
        missing = [name for name in stage.produces_results if name not in results]
        if missing:
            raise ValueError(
                f"run plan stage {stage.name!r} did not produce declared "
                f"result key(s): {sorted(missing)}"
            )

    def _call_stage_handler(
        self,
        stage: RunStage,
        results: RunResults,
    ) -> object:
        if stage.policy.timeout_s is None:
            return stage.handler(results)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(stage.handler, results)
        shutdown_wait = True
        try:
            return future.result(timeout=stage.policy.timeout_s)
        except FutureTimeoutError as exc:
            shutdown_wait = False
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"run plan stage {stage.name!r} timed out after "
                f"{stage.policy.timeout_s}s"
            ) from exc
        finally:
            if shutdown_wait:
                executor.shutdown(wait=True)

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

    def _record_stage_error(
        self,
        results: RunResults,
        stage: RunStage,
        exc: BaseException,
    ) -> None:
        errors = results.setdefault("stage_errors", [])
        if isinstance(errors, list):
            errors.append(
                {
                    "stage": stage.name,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )


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


@dataclass(frozen=True)
class RunPlanProfile:
    """Named run-level DAG profile expressed as stage names."""

    name: str
    stage_names: tuple[str, ...]
    description: str = ""
    mode: str | None = None
    stage_policies: Mapping[str, StepPolicy] = field(default_factory=dict)


def profile_stage_names(
    name: str,
    profiles: Mapping[str, RunPlanProfile],
) -> tuple[str, ...]:
    try:
        return profiles[name].stage_names
    except KeyError as exc:
        raise ValueError(f"unknown run plan profile: {name}") from exc


__all__ = [
    "RegisteredRunStage",
    "RunPlan",
    "RunPlanExecutor",
    "RunPlanProfile",
    "RunResults",
    "RunStage",
    "RunStageFactory",
    "RunStageHandler",
    "RunStageRegistry",
    "profile_stage_names",
]
