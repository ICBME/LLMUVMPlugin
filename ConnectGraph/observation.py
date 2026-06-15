from __future__ import annotations

from dataclasses import dataclass
import atexit
import os
from pathlib import Path

from .connector import ObservationContext
from .observers import Observer, observer_from_env


_context: ObservationContext | None = None
_owned_observer: Observer | None = None


@dataclass
class ObservationRuntime:
    context: ObservationContext
    observer: Observer
    topology_out: Path | None = None

    @classmethod
    def from_env(
        cls,
        *,
        observation_out: Path | None = None,
        monitoring_out: Path | None = None,
        topology_out: Path | None = None,
        run_id: str | None = None,
        round_id: str | None = None,
        stage_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> "ObservationRuntime":
        observer = observer_from_env(
            observation_out,
            monitoring_path=monitoring_out,
        )
        context = ObservationContext.from_env(observer=observer)
        run_id_env = os.getenv("CONNECTOR_OBSERVE_RUN_ID")
        round_id_env = os.getenv("CONNECTOR_OBSERVE_ROUND_ID")
        stage_id_env = os.getenv("CONNECTOR_OBSERVE_STAGE_ID")
        parent_event_id_env = os.getenv("CONNECTOR_OBSERVE_PARENT_EVENT_ID")
        if run_id_env or round_id_env or stage_id_env or parent_event_id_env:
            context = ObservationContext(
                run_id=run_id_env or context.run_id,
                round_id=round_id_env or context.round_id,
                stage_id=stage_id_env or context.stage_id,
                parent_event_id=parent_event_id_env or context.parent_event_id,
                observer=context.observer,
                strict=context.strict,
            )
        if run_id or round_id or stage_id or parent_event_id:
            context = ObservationContext(
                run_id=run_id or context.run_id,
                round_id=round_id or context.round_id,
                stage_id=stage_id or context.stage_id,
                parent_event_id=parent_event_id or context.parent_event_id,
                observer=context.observer,
                strict=context.strict,
            )
        return cls(context=context, observer=observer, topology_out=topology_out)

    def close(self) -> None:
        self.observer.close()


def observation_context_from_env(observer: Observer | None = None) -> ObservationContext:
    global _context, _owned_observer
    if _context is not None:
        return _context

    _owned_observer = observer or observer_from_env()
    base = ObservationContext.from_env(observer=_owned_observer)
    run_id = os.getenv("CONNECTOR_OBSERVE_RUN_ID") or base.run_id
    round_id = os.getenv("CONNECTOR_OBSERVE_ROUND_ID") or base.round_id
    stage_id = os.getenv("CONNECTOR_OBSERVE_STAGE_ID") or base.stage_id
    parent_event_id = os.getenv("CONNECTOR_OBSERVE_PARENT_EVENT_ID") or base.parent_event_id
    _context = ObservationContext(
        run_id=run_id,
        round_id=round_id,
        stage_id=stage_id,
        parent_event_id=parent_event_id,
        observer=base.observer,
        strict=base.strict,
    )
    atexit.register(close_observation)
    return _context


def close_observation() -> None:
    global _context, _owned_observer
    if _owned_observer is not None:
        try:
            _owned_observer.close()
        finally:
            _owned_observer = None
            _context = None


def flush_observer(observer: Observer | None) -> None:
    if observer is None:
        return
    try:
        observer.flush()
    except Exception:
        return


def observation_make_vars(
    *,
    observation_out: Path | str | None = None,
    monitoring_out: Path | str | None = None,
    topology_out: Path | str | None = None,
    observation_context: ObservationContext | None = None,
    stage_id: str,
    run_id: str | None = None,
    round_id: str | None = None,
    parent_event_id: str | None = None,
) -> list[str]:
    context = observation_context or observation_context_from_env()
    values: list[str] = []
    if observation_out is not None and str(observation_out).strip():
        values.append(f"CONNECTOR_OBSERVE_OUT={observation_out}")
    if monitoring_out is not None and str(monitoring_out).strip():
        values.append(f"CONNECTOR_MONITOR_OUT={monitoring_out}")
    if topology_out is not None and str(topology_out).strip():
        values.append(f"CONNECTOR_TOPOLOGY_OUT={topology_out}")

    resolved_run_id = run_id if run_id is not None else context.run_id
    if resolved_run_id is not None:
        values.append(f"CONNECTOR_OBSERVE_RUN_ID={resolved_run_id}")

    resolved_round_id = round_id if round_id is not None else context.round_id
    if resolved_round_id is not None:
        values.append(f"CONNECTOR_OBSERVE_ROUND_ID={resolved_round_id}")

    values.append(f"CONNECTOR_OBSERVE_STAGE_ID={stage_id}")

    resolved_parent_event_id = (
        parent_event_id if parent_event_id is not None else context.parent_event_id
    )
    if resolved_parent_event_id is not None:
        values.append(
            "CONNECTOR_OBSERVE_PARENT_EVENT_ID="
            f"{resolved_parent_event_id}"
        )
    return values


def connector_from_env(
    name: str,
    from_layer: str,
    to_layer: str,
) -> "Connector":
    from .connector import Connector

    return Connector.from_context(
        name,
        from_layer,
        to_layer,
        observation_context_from_env(),
    )


__all__ = [
    "ObservationRuntime",
    "close_observation",
    "flush_observer",
    "connector_from_env",
    "observation_make_vars",
    "observation_context_from_env",
]
