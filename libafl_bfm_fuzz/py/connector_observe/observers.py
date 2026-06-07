from __future__ import annotations

from pathlib import Path
from queue import Full, Queue
from threading import Lock, Thread
from typing import Protocol
import json

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
