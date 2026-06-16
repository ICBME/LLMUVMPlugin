from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
import inspect
import os
import time
import uuid

from .observers import NullObserver, Observer
from .schema import (
    ConnectorEvent,
    ArtifactRef,
    error_payload,
    normalize_artifact_refs,
)


OutputsSpec = (
    tuple[ArtifactRef, ...]
    | list[ArtifactRef]
    | Callable[[Any], Any]
    | None
)


@dataclass(frozen=True)
class ObservationContext:
    run_id: str | None = None
    round_id: str | None = None
    stage_id: str | None = None
    parent_event_id: str | None = None
    observer: Observer | None = None
    strict: bool = False

    def with_overrides(
        self,
        *,
        run_id: str | None = None,
        round_id: str | None = None,
        stage_id: str | None = None,
        parent_event_id: str | None = None,
        observer: Observer | None = None,
        strict: bool | None = None,
    ) -> "ObservationContext":
        return replace(
            self,
            run_id=self.run_id if run_id is None else run_id,
            round_id=self.round_id if round_id is None else round_id,
            stage_id=self.stage_id if stage_id is None else stage_id,
            parent_event_id=(
                self.parent_event_id
                if parent_event_id is None
                else parent_event_id
            ),
            observer=self.observer if observer is None else observer,
            strict=self.strict if strict is None else strict,
        )

    @classmethod
    def from_env(cls, observer: Observer | None = None) -> "ObservationContext":
        run_id = os.getenv("OBSERVATION_RUN_ID")
        round_id = os.getenv("OBSERVATION_ROUND_ID")
        stage_id = os.getenv("OBSERVATION_STAGE_ID")
        parent_event_id = os.getenv("OBSERVATION_PARENT_EVENT_ID")
        strict = os.getenv("STRICT_OBSERVATION", "0") in {"1", "true", "TRUE", "yes", "YES"}
        return cls(
            run_id=run_id,
            round_id=round_id,
            stage_id=stage_id,
            parent_event_id=parent_event_id,
            observer=observer,
            strict=strict,
        )


class Connector:
    def __init__(
        self,
        name: str,
        from_layer: str,
        to_layer: str,
        *,
        observer: Observer | None = None,
        run_id: str | None = None,
        round_id: str | None = None,
        stage_id: str | None = None,
        parent_event_id: str | None = None,
        strict_observation: bool = False,
    ):
        self.name = name
        self.from_layer = from_layer
        self.to_layer = to_layer
        self.observer = observer or NullObserver()
        self.run_id = run_id
        self.round_id = round_id
        self.stage_id = stage_id
        self.parent_event_id = parent_event_id
        self.strict_observation = strict_observation
        self._enabled = not isinstance(self.observer, NullObserver)

    @classmethod
    def from_context(
        cls,
        name: str,
        from_layer: str,
        to_layer: str,
        context: ObservationContext,
    ) -> "Connector":
        return cls(
            name,
            from_layer,
            to_layer,
            observer=context.observer,
            run_id=context.run_id,
            round_id=context.round_id,
            stage_id=context.stage_id,
            parent_event_id=context.parent_event_id,
            strict_observation=context.strict,
        )

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        inputs: Any = None,
        outputs: OutputsSpec = None,
        metrics: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        if not self._enabled:
            return fn(*args, **kwargs)

        input_refs = normalize_artifact_refs(inputs)
        metadata = self._metadata(metadata)
        span_id = str(uuid.uuid4())
        started_at_ns = time.time_ns()
        perf_start_ns = time.perf_counter_ns()
        self._emit(
            ConnectorEvent(
                event_type="connector.started",
                connector=self.name,
                from_layer=self.from_layer,
                to_layer=self.to_layer,
                run_id=self.run_id,
                span_id=span_id,
                started_at_ns=started_at_ns,
                inputs=input_refs,
                metadata=metadata,
                status="running",
            )
        )
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            duration_ms = _duration_ms(perf_start_ns)
            self._emit(
                ConnectorEvent(
                    event_type="connector.failed",
                    connector=self.name,
                    from_layer=self.from_layer,
                    to_layer=self.to_layer,
                    run_id=self.run_id,
                    span_id=span_id,
                    started_at_ns=started_at_ns,
                    duration_ms=duration_ms,
                    inputs=input_refs,
                    metadata=metadata,
                    status="failed",
                    error=error_payload(exc),
                )
            )
            raise

        output_refs = self._resolve_outputs(outputs, result)
        metric_values = self._resolve_metrics(metrics, result)
        duration_ms = _duration_ms(perf_start_ns)
        self._emit(
            ConnectorEvent(
                event_type="connector.finished",
                connector=self.name,
                from_layer=self.from_layer,
                to_layer=self.to_layer,
                run_id=self.run_id,
                span_id=span_id,
                started_at_ns=started_at_ns,
                duration_ms=duration_ms,
                inputs=input_refs,
                outputs=output_refs,
                metrics=metric_values,
                metadata=metadata,
                status="ok",
            )
        )
        return result

    async def run_async(
        self,
        fn: Callable[..., Any],
        *args: Any,
        inputs: Any = None,
        outputs: OutputsSpec = None,
        metrics: dict[str, Any] | Callable[[Any], dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        if not self._enabled:
            return await _maybe_await(fn(*args, **kwargs))

        input_refs = normalize_artifact_refs(inputs)
        metadata = self._metadata(metadata)
        span_id = str(uuid.uuid4())
        started_at_ns = time.time_ns()
        perf_start_ns = time.perf_counter_ns()
        self._emit(
            ConnectorEvent(
                event_type="connector.started",
                connector=self.name,
                from_layer=self.from_layer,
                to_layer=self.to_layer,
                run_id=self.run_id,
                span_id=span_id,
                started_at_ns=started_at_ns,
                inputs=input_refs,
                metadata=metadata,
                status="running",
            )
        )
        try:
            result = await _maybe_await(fn(*args, **kwargs))
        except Exception as exc:
            duration_ms = _duration_ms(perf_start_ns)
            self._emit(
                ConnectorEvent(
                    event_type="connector.failed",
                    connector=self.name,
                    from_layer=self.from_layer,
                    to_layer=self.to_layer,
                    run_id=self.run_id,
                    span_id=span_id,
                    started_at_ns=started_at_ns,
                    duration_ms=duration_ms,
                    inputs=input_refs,
                    metadata=metadata,
                    status="failed",
                    error=error_payload(exc),
                )
            )
            raise

        output_refs = self._resolve_outputs(outputs, result)
        metric_values = self._resolve_metrics(metrics, result)
        duration_ms = _duration_ms(perf_start_ns)
        self._emit(
            ConnectorEvent(
                event_type="connector.finished",
                connector=self.name,
                from_layer=self.from_layer,
                to_layer=self.to_layer,
                run_id=self.run_id,
                span_id=span_id,
                started_at_ns=started_at_ns,
                duration_ms=duration_ms,
                inputs=input_refs,
                outputs=output_refs,
                metrics=metric_values,
                metadata=metadata,
                status="ok",
            )
        )
        return result

    def _emit(self, event: ConnectorEvent) -> None:
        try:
            self.observer.on_event(event)
        except Exception:
            if self.strict_observation:
                raise

    def _metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        value = dict(metadata or {})
        if self.run_id is not None:
            value.setdefault("run_id", self.run_id)
        if self.round_id is not None:
            value.setdefault("round_id", self.round_id)
        if self.stage_id is not None:
            value.setdefault("stage_id", self.stage_id)
        if self.parent_event_id is not None:
            value.setdefault("parent_event_id", self.parent_event_id)
        return value

    def _resolve_outputs(self, outputs: OutputsSpec, result: Any) -> tuple[ArtifactRef, ...]:
        if callable(outputs):
            try:
                return normalize_artifact_refs(outputs(result))
            except Exception:
                if self.strict_observation:
                    raise
                return ()
        return normalize_artifact_refs(outputs)

    def _resolve_metrics(
        self,
        metrics: dict[str, Any] | Callable[[Any], dict[str, Any]] | None,
        result: Any,
    ) -> dict[str, Any]:
        if metrics is None:
            return {}
        if callable(metrics):
            try:
                value = metrics(result)
            except Exception:
                if self.strict_observation:
                    raise
                return {}
            return dict(value or {})
        return dict(metrics)


def _duration_ms(start_ns: int) -> float:
    return round((time.perf_counter_ns() - start_ns) / 1_000_000, 6)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
