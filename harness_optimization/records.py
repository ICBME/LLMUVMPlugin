from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol


class HarnessRecordProjectorProtocol(Protocol):
    def project(
        self,
        events: Iterable[tuple[int, dict[str, Any]]],
        *,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


def case_key(record: dict[str, Any]) -> str | int | None:
    case_id_value = record.get("case_id")
    if case_id_value is not None:
        return str(case_id_value)
    index = record.get("case_index")
    if isinstance(index, int):
        return index
    return None


def record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "span_id": record.get("span_id"),
        "connector": record.get("connector"),
        "step": record.get("step"),
        "from_layer": record.get("from_layer"),
        "to_layer": record.get("to_layer"),
        "status": record.get("status"),
        "duration_ms": record.get("duration_ms"),
        "case_index": record.get("case_index"),
        "case_id": record.get("case_id"),
        "directive_id": record.get("directive_id"),
        "corpus_sha256": record.get("corpus_sha256"),
        "error": record.get("error"),
        "evidence": record.get("evidence"),
    }


def first_present(values: Iterable[Any]) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def optional_path(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def number(value: Any) -> float | int | None:
    return value if isinstance(value, int | float) else None
