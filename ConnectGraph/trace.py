from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any


STARTED_EVENT_TYPE = "connector.started"
FINAL_EVENT_TYPES = {"connector.finished", "connector.failed"}


def read_jsonl_events(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    events: list[tuple[int, dict[str, Any]]] = []
    malformed_line_count = 0
    if not path.exists():
        return events, malformed_line_count
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                malformed_line_count += 1
                continue
            if isinstance(value, dict):
                events.append((line_no, value))
    return events, malformed_line_count


def read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def event_status(event: dict[str, Any]) -> str:
    status = event.get("status")
    if isinstance(status, str) and status:
        return status
    return "failed" if event.get("event_type") == "connector.failed" else "ok"


def event_span_id(event: dict[str, Any]) -> str | None:
    value = event.get("span_id")
    if isinstance(value, str) and value:
        return value
    metadata = _mapping(event.get("metadata"))
    value = metadata.get("span_id")
    if isinstance(value, str) and value:
        return value
    return None


def trace_quality(
    events: list[tuple[int, dict[str, Any]]],
    malformed_line_count: int,
    *,
    ignored_hanging_connectors: set[str] | None = None,
) -> dict[str, Any]:
    ignored_hanging_connectors = ignored_hanging_connectors or set()
    started_by_span: dict[str, tuple[int, dict[str, Any]]] = {}
    finals_by_span: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    missing_span_id_count = 0
    duplicate_started_spans: list[dict[str, Any]] = []

    for line_no, event in events:
        event_type = event.get("event_type")
        span_id = event_span_id(event)
        if span_id is None:
            missing_span_id_count += 1
            continue
        if event_type == STARTED_EVENT_TYPE:
            if span_id in started_by_span:
                duplicate_started_spans.append(
                    span_event_summary(line_no, span_id, event)
                )
            started_by_span.setdefault(span_id, (line_no, event))
        elif event_type in FINAL_EVENT_TYPES:
            finals_by_span[span_id].append((line_no, event))

    all_hanging_spans = [
        span_event_summary(line_no, span_id, event)
        for span_id, (line_no, event) in sorted(started_by_span.items())
        if span_id not in finals_by_span
    ]
    hanging_spans = [
        item
        for item in all_hanging_spans
        if str(item.get("connector", "")) not in ignored_hanging_connectors
    ]
    ignored_hanging_spans = [
        item
        for item in all_hanging_spans
        if str(item.get("connector", "")) in ignored_hanging_connectors
    ]
    orphan_final_spans = [
        span_event_summary(line_no, span_id, event)
        for span_id, items in sorted(finals_by_span.items())
        if span_id not in started_by_span
        for line_no, event in items[:1]
    ]
    duplicate_final_spans = [
        {
            "span_id": span_id,
            "final_event_count": len(items),
            "examples": [
                span_event_summary(line_no, span_id, event)
                for line_no, event in items[:5]
            ],
        }
        for span_id, items in sorted(finals_by_span.items())
        if len(items) > 1
    ]
    event_types = defaultdict(int)
    for _line_no, event in events:
        event_types[str(event.get("event_type", ""))] += 1

    return {
        "event_count": len(events),
        "malformed_line_count": malformed_line_count,
        "started_count": int(event_types.get(STARTED_EVENT_TYPE, 0)),
        "finished_count": int(event_types.get("connector.finished", 0)),
        "failed_count": int(event_types.get("connector.failed", 0)),
        "final_count": sum(int(event_types.get(name, 0)) for name in FINAL_EVENT_TYPES),
        "span_count": len(set(started_by_span) | set(finals_by_span)),
        "missing_span_id_count": missing_span_id_count,
        "hanging_span_count": len(hanging_spans),
        "ignored_hanging_span_count": len(ignored_hanging_spans),
        "orphan_final_count": len(orphan_final_spans),
        "duplicate_started_span_count": len(duplicate_started_spans),
        "duplicate_final_span_count": len(duplicate_final_spans),
        "hanging_spans": hanging_spans[:20],
        "ignored_hanging_spans": ignored_hanging_spans[:20],
        "orphan_final_spans": orphan_final_spans[:20],
        "duplicate_started_spans": duplicate_started_spans[:20],
        "duplicate_final_spans": duplicate_final_spans[:20],
    }


def span_event_summary(
    line_no: int,
    span_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    metadata = _mapping(event.get("metadata"))
    return {
        "span_id": span_id,
        "event_line": line_no,
        "event_type": event.get("event_type"),
        "connector": event.get("connector"),
        "step": metadata.get("step"),
        "from_layer": event.get("from_layer"),
        "to_layer": event.get("to_layer"),
        "status": event.get("status"),
        "timestamp_ns": event.get("timestamp_ns"),
        "started_at_ns": event.get("started_at_ns"),
    }


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
