from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from ConnectGraph.trace import event_span_id, event_status

from .metadata import HarnessMetadataExtractorProtocol


HARNESS_EXECUTION_RECORD_KIND = "harness_optimization.harness_execution_record"


class HarnessRecordProjectorProtocol(Protocol):
    def project(
        self,
        events: Iterable[tuple[int, dict[str, Any]]],
        *,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class HarnessExecutionRecordProjector:
    observation_events: Path
    metadata_extractor: HarnessMetadataExtractorProtocol
    monitoring: Path | None = None
    round_manifest: Path | None = None
    campaign_manifest: Path | None = None
    kind: str = HARNESS_EXECUTION_RECORD_KIND

    def project(
        self,
        events: Iterable[tuple[int, dict[str, Any]]],
        *,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            self.record_from_event(
                event,
                line_no=line_no,
                round_manifest=round_manifest,
                campaign_manifest=campaign_manifest,
            )
            for line_no, event in events
        ]

    def record_from_event(
        self,
        event: dict[str, Any],
        *,
        line_no: int,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = mapping(event.get("metadata"))
        projected_metadata = self.metadata_extractor.extract(metadata)
        return {
            "schema_version": 1,
            "kind": self.kind,
            "run_id": event.get("run_id")
            or metadata.get("run_id")
            or round_manifest.get("run_id")
            or campaign_manifest.get("run_id"),
            "round_id": metadata.get("round_id") or round_manifest.get("round_id"),
            "stage_id": metadata.get("stage_id"),
            "case_index": projected_metadata.get("case_index"),
            "case_id": projected_metadata.get("case_id"),
            "directive_id": projected_metadata.get("directive_id"),
            "corpus_sha256": projected_metadata.get("corpus_sha256"),
            "span_id": event_span_id(event),
            "target": metadata.get("target")
            or round_manifest.get("target")
            or campaign_manifest.get("target"),
            "mode": metadata.get("mode") or round_manifest.get("mode"),
            "step": metadata.get("step"),
            "connector": str(event.get("connector", "")),
            "from_layer": str(event.get("from_layer", "")),
            "to_layer": str(event.get("to_layer", "")),
            "status": event_status(event),
            "event_type": event.get("event_type"),
            "timestamp_ns": event.get("timestamp_ns"),
            "started_at_ns": event.get("started_at_ns"),
            "duration_ms": number(event.get("duration_ms")),
            "inputs": list_value(event.get("inputs")),
            "outputs": list_value(event.get("outputs")),
            "metrics": mapping(event.get("metrics")),
            "metadata": metadata,
            "error": event.get("error") if isinstance(event.get("error"), dict) else None,
            "evidence": {
                "event_source": str(self.observation_events),
                "event_line": line_no,
                **optional_path("round_manifest", self.round_manifest),
                **optional_path("campaign_manifest", self.campaign_manifest),
                **optional_path("monitoring", self.monitoring),
            },
        }


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


__all__ = [
    "HARNESS_EXECUTION_RECORD_KIND",
    "HarnessExecutionRecordProjector",
    "HarnessRecordProjectorProtocol",
    "case_key",
    "first_present",
    "list_value",
    "mapping",
    "number",
    "optional_path",
    "record_summary",
]
