from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Any

from ConnectGraph.connector import Connector, ObservationContext
from ConnectGraph.schema import normalize_artifact_refs
from ConnectGraph.topology import ConnectorEdge, PipelineTopology, write_topology


@dataclass(frozen=True)
class StepPolicy:
    fail_open_observation: bool = True
    fail_main_on_step_error: bool = True
    timeout_s: float | None = None


@dataclass
class PipelineContext:
    run_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepSpec:
    name: str
    connector: str
    handler: Callable[[PipelineContext], Any]
    input_roles: tuple[str, ...] = ()
    output_roles: tuple[str, ...] = ()
    metrics: Callable[[Any], dict[str, Any]] | dict[str, Any] | None = None
    metadata: dict[str, Any] | Callable[[PipelineContext], dict[str, Any]] | None = None
    async_step: bool = False
    policy: StepPolicy = field(default_factory=StepPolicy)
    result_key: str | None = None


class PipelineOrchestrator:
    def __init__(
        self,
        topology: PipelineTopology,
        observation_context: ObservationContext | None = None,
        *,
        topology_out: Path | None = None,
    ):
        self.topology = topology
        self.observation_context = observation_context or ObservationContext()
        self.topology_out = topology_out
        self._edges = validate_topology(topology)

    def run(
        self,
        steps: list[StepSpec] | tuple[StepSpec, ...],
        context: PipelineContext | None = None,
    ) -> PipelineContext:
        pipeline_context = self._prepare_context(context)
        self.write_topology()
        for step in steps:
            self.run_step(step, pipeline_context)
        return pipeline_context

    async def run_async(
        self,
        steps: list[StepSpec] | tuple[StepSpec, ...],
        context: PipelineContext | None = None,
    ) -> PipelineContext:
        pipeline_context = self._prepare_context(context)
        self.write_topology()
        for step in steps:
            await self.run_step_async(step, pipeline_context)
        return pipeline_context

    def run_step(self, step: StepSpec, context: PipelineContext) -> Any:
        if step.async_step:
            raise ValueError(
                f"step {step.name!r} is marked async; use run_step_async() or run_async()"
            )
        edge = self._validate_step(step)
        metadata = self._metadata(step, context)
        connector = self._connector(edge, step)
        try:
            result = connector.run(
                self._call_handler,
                step,
                context,
                inputs=self._artifact_refs(context, step.input_roles),
                outputs=self._outputs(context, step.output_roles),
                metrics=step.metrics,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - policy decides whether orchestration continues
            if step.policy.fail_main_on_step_error:
                raise
            self._record_step_error(context, step, exc)
            return None
        self._store_result(context, step, result)
        return result

    async def run_step_async(self, step: StepSpec, context: PipelineContext) -> Any:
        edge = self._validate_step(step)
        metadata = self._metadata(step, context)
        connector = self._connector(edge, step)
        try:
            result = await connector.run_async(
                self._call_handler_async,
                step,
                context,
                inputs=self._artifact_refs(context, step.input_roles),
                outputs=self._outputs(context, step.output_roles),
                metrics=step.metrics,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 - policy decides whether orchestration continues
            if step.policy.fail_main_on_step_error:
                raise
            self._record_step_error(context, step, exc)
            return None
        self._store_result(context, step, result)
        return result

    def _prepare_context(self, context: PipelineContext | None) -> PipelineContext:
        if context is None:
            return PipelineContext(run_id=self.observation_context.run_id)
        if context.run_id is None:
            context.run_id = self.observation_context.run_id
        return context

    def _validate_step(self, step: StepSpec) -> ConnectorEdge:
        try:
            edge = self._edges[step.connector]
        except KeyError as exc:
            raise ValueError(
                f"unknown connector in step {step.name!r}: {step.connector}"
            ) from exc
        missing_inputs = set(edge.input_roles) - set(step.input_roles)
        if missing_inputs:
            raise ValueError(
                f"step {step.name!r} does not declare required input role(s): "
                f"{sorted(missing_inputs)}"
            )
        missing_outputs = set(edge.output_roles) - set(step.output_roles)
        if missing_outputs:
            raise ValueError(
                f"step {step.name!r} does not declare required output role(s): "
                f"{sorted(missing_outputs)}"
            )
        return edge

    def _connector(self, edge: ConnectorEdge, step: StepSpec) -> Connector:
        return Connector(
            edge.name,
            edge.from_layer,
            edge.to_layer,
            observer=self.observation_context.observer,
            run_id=self.observation_context.run_id,
            round_id=self.observation_context.round_id,
            stage_id=self.observation_context.stage_id,
            parent_event_id=self.observation_context.parent_event_id,
            strict_observation=(
                self.observation_context.strict or not step.policy.fail_open_observation
            ),
        )

    def _metadata(self, step: StepSpec, context: PipelineContext) -> dict[str, Any]:
        value = dict(context.metadata)
        value["step"] = step.name
        if callable(step.metadata):
            value.update(step.metadata(context))
        elif step.metadata:
            value.update(step.metadata)
        return value

    def _artifact_refs(self, context: PipelineContext, roles: tuple[str, ...]) -> dict[str, Path]:
        artifacts = {}
        for role in roles:
            try:
                artifacts[role] = context.artifacts[role]
            except KeyError as exc:
                raise KeyError(f"missing artifact role {role!r}") from exc
        normalize_artifact_refs(artifacts)
        return artifacts

    def _outputs(self, context: PipelineContext, roles: tuple[str, ...]) -> dict[str, Path]:
        outputs = {}
        for role in roles:
            try:
                outputs[role] = context.artifacts[role]
            except KeyError as exc:
                raise KeyError(f"missing output artifact role {role!r}") from exc
        return outputs

    def _call_handler(self, step: StepSpec, context: PipelineContext) -> Any:
        if step.policy.timeout_s is None:
            return step.handler(context)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(step.handler, context)
        shutdown_wait = True
        try:
            return future.result(timeout=step.policy.timeout_s)
        except FutureTimeoutError as exc:
            shutdown_wait = False
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"step {step.name!r} timed out after {step.policy.timeout_s}s"
            ) from exc
        finally:
            if shutdown_wait:
                executor.shutdown(wait=True)

    async def _call_handler_async(self, step: StepSpec, context: PipelineContext) -> Any:
        value = step.handler(context)
        if step.policy.timeout_s is None:
            return await _maybe_await(value)
        return await asyncio.wait_for(_maybe_await(value), timeout=step.policy.timeout_s)

    def _store_result(self, context: PipelineContext, step: StepSpec, result: Any) -> None:
        context.values[step.result_key or step.name] = result

    def _record_step_error(
        self,
        context: PipelineContext,
        step: StepSpec,
        exc: BaseException,
    ) -> None:
        errors = context.metadata.setdefault("step_errors", [])
        if isinstance(errors, list):
            errors.append(
                {
                    "step": step.name,
                    "connector": step.connector,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    def write_topology(self) -> None:
        if self.topology_out is None:
            return
        write_topology(self.topology, self.topology_out)


def external_command_step(
    *,
    name: str,
    connector: str,
    command: list[str],
    input_roles: tuple[str, ...] = (),
    output_roles: tuple[str, ...] = (),
    metadata: dict[str, Any] | Callable[[PipelineContext], dict[str, Any]] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> StepSpec:
    def handler(_context: PipelineContext) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            check=False,
            cwd=cwd,
            env=env,
            shell=shell,
            capture_output=False,
        )

    return StepSpec(
        name=name,
        connector=connector,
        handler=handler,
        input_roles=input_roles,
        output_roles=output_roles,
        metadata=metadata,
    )


def validate_topology(topology: PipelineTopology) -> dict[str, ConnectorEdge]:
    edges: dict[str, ConnectorEdge] = {}
    for edge in topology.connectors:
        if edge.name in edges:
            raise ValueError(f"duplicate connector name {edge.name!r}")
        edges[edge.name] = edge
    return edges


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


__all__ = [
    "PipelineContext",
    "PipelineOrchestrator",
    "StepPolicy",
    "StepSpec",
    "external_command_step",
    "validate_topology",
]
