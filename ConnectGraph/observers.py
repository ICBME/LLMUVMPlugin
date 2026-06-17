from __future__ import annotations

from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
from typing import Protocol
import json
import os

from .schema import ConnectorEvent


class Observer(Protocol):
    def on_event(self, event: ConnectorEvent) -> None:
        ...

    def flush(self) -> None:
        ...

    def close(self) -> None:
        ...


class NullObserver:
    def on_event(self, event: ConnectorEvent) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class JsonlObserver:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()
        self._file = None

    def on_event(self, event: ConnectorEvent) -> None:
        line = json.dumps(event.to_json(), sort_keys=True) + "\n"
        with self._lock:
            file = self._open()
            file.write(line)

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None

    def _open(self):
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
        return self._file


class CompositeObserver:
    def __init__(self, observers: list[Observer] | tuple[Observer, ...]):
        self.observers = tuple(observers)

    def on_event(self, event: ConnectorEvent) -> None:
        errors = []
        for observer in self.observers:
            try:
                observer.on_event(event)
            except Exception as exc:  # noqa: BLE001 - isolate observers from each other
                errors.append(exc)
        if errors:
            raise errors[0]

    def flush(self) -> None:
        errors = []
        for observer in self.observers:
            try:
                observer.flush()
            except Exception as exc:  # noqa: BLE001 - isolate observers from each other
                errors.append(exc)
        if errors:
            raise errors[0]

    def close(self) -> None:
        errors = []
        for observer in self.observers:
            try:
                observer.close()
            except Exception as exc:  # noqa: BLE001 - isolate observers from each other
                errors.append(exc)
        if errors:
            raise errors[0]


class MonitoringObserver:
    """Aggregate connector-level health metrics without touching main results."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._lock = Lock()
        self._connectors: dict[str, dict] = {}
        self._edges: dict[str, dict] = {}
        self._event_count = 0
        self._load_existing_snapshot()

    def on_event(self, event: ConnectorEvent) -> None:
        with self._lock:
            self._event_count += 1
            connector = self._connectors.setdefault(
                event.connector,
                {
                    "connector": event.connector,
                    "from_layer": event.from_layer,
                    "to_layer": event.to_layer,
                    "started": 0,
                    "finished": 0,
                    "failed": 0,
                    "total_duration_ms": 0.0,
                    "last_status": None,
                    "last_error": None,
                    "metrics": {},
                },
            )
            edge_key = f"{event.from_layer}->{event.to_layer}"
            edge = self._edges.setdefault(
                edge_key,
                {
                    "from_layer": event.from_layer,
                    "to_layer": event.to_layer,
                    "connectors": set(),
                    "event_count": 0,
                },
            )
            edge["connectors"].add(event.connector)
            edge["event_count"] += 1

            if event.event_type == "connector.started":
                connector["started"] += 1
            elif event.event_type == "connector.finished":
                connector["finished"] += 1
            elif event.event_type == "connector.failed":
                connector["failed"] += 1
                connector["last_error"] = event.error
            if event.duration_ms is not None:
                connector["total_duration_ms"] = round(
                    float(connector["total_duration_ms"]) + event.duration_ms,
                    6,
                )
            if event.status is not None:
                connector["last_status"] = event.status
            if event.metrics:
                connector["metrics"] = event.metrics

    def flush(self) -> None:
        if self.path is None:
            return
        snapshot = self.snapshot()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    def close(self) -> None:
        self.flush()

    def snapshot(self) -> dict:
        with self._lock:
            connectors = []
            for item in self._connectors.values():
                row = dict(item)
                if row["finished"] or row["failed"]:
                    completed = row["finished"] + row["failed"]
                    row["avg_duration_ms"] = round(row["total_duration_ms"] / completed, 6)
                else:
                    row["avg_duration_ms"] = 0.0
                connectors.append(row)
            edges = []
            for item in self._edges.values():
                row = dict(item)
                row["connectors"] = sorted(row["connectors"])
                edges.append(row)
            return {
                "schema_version": 1,
                "event_count": self._event_count,
                "connector_count": len(connectors),
                "failed_connector_count": sum(1 for item in connectors if item["failed"]),
                "connectors": sorted(connectors, key=lambda item: item["connector"]),
                "edges": sorted(edges, key=lambda item: (item["from_layer"], item["to_layer"])),
            }

    def _load_existing_snapshot(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            snapshot = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(snapshot, dict):
            return

        self._event_count = int(snapshot.get("event_count", 0) or 0)
        for item in snapshot.get("connectors", []):
            if not isinstance(item, dict) or "connector" not in item:
                continue
            row = {
                "connector": str(item.get("connector", "")),
                "from_layer": str(item.get("from_layer", "")),
                "to_layer": str(item.get("to_layer", "")),
                "started": int(item.get("started", 0) or 0),
                "finished": int(item.get("finished", 0) or 0),
                "failed": int(item.get("failed", 0) or 0),
                "total_duration_ms": float(item.get("total_duration_ms", 0.0) or 0.0),
                "last_status": item.get("last_status"),
                "last_error": item.get("last_error"),
                "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
            }
            self._connectors[row["connector"]] = row
        for item in snapshot.get("edges", []):
            if not isinstance(item, dict):
                continue
            from_layer = str(item.get("from_layer", ""))
            to_layer = str(item.get("to_layer", ""))
            if not from_layer or not to_layer:
                continue
            connectors = item.get("connectors", [])
            self._edges[f"{from_layer}->{to_layer}"] = {
                "from_layer": from_layer,
                "to_layer": to_layer,
                "connectors": set(str(connector) for connector in connectors),
                "event_count": int(item.get("event_count", 0) or 0),
            }


class AsyncObserver:
    def __init__(self, observer: Observer, *, max_queue_size: int = 1024):
        self.observer = observer
        self.dropped_event_count = 0
        self._queue: Queue[ConnectorEvent | None] = Queue(maxsize=max(1, max_queue_size))
        self._closed = False
        self._thread = Thread(target=self._worker, name="fuzz-observe-async", daemon=True)
        self._thread.start()

    def on_event(self, event: ConnectorEvent) -> None:
        if self._closed:
            self.dropped_event_count += 1
            return
        try:
            self._queue.put_nowait(event)
        except Full:
            self.dropped_event_count += 1

    def flush(self) -> None:
        self._queue.join()
        self.observer.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=2)
        self.observer.close()

    def _worker(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                try:
                    self.observer.on_event(event)
                except Exception:
                    pass
            finally:
                self._queue.task_done()


def observer_from_env(
    path: str | Path | None = None,
    *,
    monitoring_path: str | Path | None = None,
) -> Observer:
    output_path = path or os.getenv("CONNECTOR_OBSERVE_OUT")
    monitor_path = monitoring_path or os.getenv("CONNECTOR_MONITOR_OUT")
    observers: list[Observer] = []
    if output_path is not None and str(output_path).strip():
        observer: Observer = JsonlObserver(output_path)
        if os.getenv("CONNECTOR_OBSERVE_ASYNC", "1") not in {"0", "false", "FALSE", "no", "NO"}:
            try:
                queue_size = int(os.getenv("CONNECTOR_OBSERVE_QUEUE", "1024"))
            except ValueError:
                queue_size = 1024
            observer = AsyncObserver(observer, max_queue_size=queue_size)
        observers.append(observer)
    if monitor_path is not None and str(monitor_path).strip():
        observers.append(MonitoringObserver(monitor_path))
    if not observers:
        return NullObserver()
    if len(observers) == 1:
        return observers[0]
    return CompositeObserver(observers)
