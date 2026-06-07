from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
import os
import time

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
    observer: Observer | None = None
    strict: bool = False

    @classmethod
    def from_env(cls, observer: Observer | None = None) -> "ObservationContext":
        run_id = os.getenv("OBSERVATION_RUN_ID")
        strict = os.getenv("STRICT_OBSERVATION", "0") in {"1", "true", "TRUE", "yes", "YES"}
        return cls(run_id=run_id, observer=observer, strict=strict)


class Connector:
    def __init__(
        self,
        name: str,
        from_layer: str,
        to_layer: str,
        *,
        observer: Observer | None = None,
        run_id: str | None = None,
        strict_observation: bool = False,
    ):
        self.name = name
        self.from_layer = from_layer
        self.to_layer = to_layer
        self.observer = observer or NullObserver()
        self.run_id = run_id
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
        metadata = dict(metadata or {})
        started_at_ns = time.time_ns()
        perf_start_ns = time.perf_counter_ns()
        self._emit(
            ConnectorEvent(
                event_type="connector.started",
                connector=self.name,
                from_layer=self.from_layer,
                to_layer=self.to_layer,
                run_id=self.run_id,
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
