from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from connector_observe import ObservationContext, observer_from_env
from connector_observe.observers import Observer


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
    ) -> "ObservationRuntime":
        observer = observer_from_env(
            observation_out,
            monitoring_path=monitoring_out,
        )
        context = ObservationContext.from_env(observer=observer)
        if run_id:
            context = ObservationContext(
                run_id=run_id,
                round_id=context.round_id,
                stage_id=context.stage_id,
                parent_event_id=context.parent_event_id,
                observer=context.observer,
                strict=context.strict,
            )
        return cls(context=context, observer=observer, topology_out=topology_out)

    def close(self) -> None:
        self.observer.close()
