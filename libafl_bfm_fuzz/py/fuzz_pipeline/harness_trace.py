from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
FINAL_EVENT_TYPES = {"connector.finished", "connector.failed"}


@dataclass(frozen=True)
class HarnessTraceOutputs:
    records: Path
    evaluation: Path
    llm_dataset: Path

    @classmethod
    def from_evaluation_path(cls, path: Path) -> "HarnessTraceOutputs":
        return cls(
            records=path.with_name(f"{path.stem}_harness_records.jsonl"),
            evaluation=path.with_name(f"{path.stem}_harness_evaluation.json"),
            llm_dataset=path.with_name(f"{path.stem}_llm_dataset.jsonl"),
        )

    def to_json(self) -> dict[str, str]:
        return {
            "harness_execution_records": str(self.records),
            "harness_evaluation": str(self.evaluation),
            "llm_optimization_dataset": str(self.llm_dataset),
        }


@dataclass(frozen=True)
class HarnessTraceResult:
    records: list[dict[str, Any]]
    evaluation: dict[str, Any]
    llm_dataset: list[dict[str, Any]]


@dataclass(frozen=True)
class HarnessTraceBuilder:
    observation_events: Path
    monitoring: Path | None = None
    round_manifest: Path | None = None
    campaign_manifest: Path | None = None
    max_llm_records: int = 200

    def build(self) -> HarnessTraceResult:
        round_manifest = _read_json_object(self.round_manifest)
        campaign_manifest = _read_json_object(self.campaign_manifest)
        monitoring = _read_json_object(self.monitoring)
        records = [
            self._record_from_event(
                event,
                line_no=line_no,
                round_manifest=round_manifest,
                campaign_manifest=campaign_manifest,
            )
            for line_no, event in _iter_jsonl_objects(self.observation_events)
            if event.get("event_type") in FINAL_EVENT_TYPES
        ]
        evaluation = self._evaluation_payload(
            records,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
            monitoring=monitoring,
        )
        llm_dataset = self._llm_dataset(records, evaluation)
        return HarnessTraceResult(
            records=records,
            evaluation=evaluation,
            llm_dataset=llm_dataset,
        )

    def write(self, outputs: HarnessTraceOutputs) -> HarnessTraceResult:
        result = self.build()
        outputs.records.parent.mkdir(parents=True, exist_ok=True)
        outputs.records.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in result.records),
            encoding="utf-8",
        )
        outputs.evaluation.parent.mkdir(parents=True, exist_ok=True)
        outputs.evaluation.write_text(
            json.dumps(result.evaluation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        outputs.llm_dataset.parent.mkdir(parents=True, exist_ok=True)
        outputs.llm_dataset.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in result.llm_dataset),
            encoding="utf-8",
        )
        return result

    def _record_from_event(
        self,
        event: dict[str, Any],
        *,
        line_no: int,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = _mapping(event.get("metadata"))
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "libafl_bfm_fuzz.harness_execution_record",
            "run_id": event.get("run_id")
            or metadata.get("run_id")
            or round_manifest.get("run_id")
            or campaign_manifest.get("run_id"),
            "round_id": metadata.get("round_id") or round_manifest.get("round_id"),
            "stage_id": metadata.get("stage_id"),
            "case_index": _case_index(metadata),
            "target": metadata.get("target")
            or round_manifest.get("target")
            or campaign_manifest.get("target"),
            "mode": metadata.get("mode") or round_manifest.get("mode"),
            "step": metadata.get("step"),
            "connector": str(event.get("connector", "")),
            "from_layer": str(event.get("from_layer", "")),
            "to_layer": str(event.get("to_layer", "")),
            "status": _status(event),
            "event_type": event.get("event_type"),
            "timestamp_ns": event.get("timestamp_ns"),
            "started_at_ns": event.get("started_at_ns"),
            "duration_ms": _number(event.get("duration_ms")),
            "inputs": _list(event.get("inputs")),
            "outputs": _list(event.get("outputs")),
            "metrics": _mapping(event.get("metrics")),
            "metadata": metadata,
            "error": event.get("error") if isinstance(event.get("error"), dict) else None,
            "evidence": {
                "event_source": str(self.observation_events),
                "event_line": line_no,
                **_optional_path("round_manifest", self.round_manifest),
                **_optional_path("campaign_manifest", self.campaign_manifest),
                **_optional_path("monitoring", self.monitoring),
            },
        }

    def _evaluation_payload(
        self,
        records: list[dict[str, Any]],
        *,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
        monitoring: dict[str, Any],
    ) -> dict[str, Any]:
        connector_stats = _connector_stats(records)
        module_stats = _module_stats(records)
        failure_clusters = _failure_clusters(records)
        slowest = sorted(
            records,
            key=lambda item: float(item.get("duration_ms") or 0.0),
            reverse=True,
        )[:10]
        failed_records = [item for item in records if item.get("status") != "ok"]
        case_records = [item for item in records if item.get("case_index") is not None]
        case_ids = sorted({item["case_index"] for item in case_records})
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "libafl_bfm_fuzz.harness_evaluation",
            "sources": {
                "observation_events": str(self.observation_events),
                **_optional_path("monitoring", self.monitoring),
                **_optional_path("round_manifest", self.round_manifest),
                **_optional_path("campaign_manifest", self.campaign_manifest),
            },
            "run_id": _first_present(
                [round_manifest.get("run_id"), campaign_manifest.get("run_id")]
                + [record.get("run_id") for record in records]
            ),
            "round_id": _first_present(
                [round_manifest.get("round_id")]
                + [record.get("round_id") for record in records]
            ),
            "target": _first_present(
                [round_manifest.get("target"), campaign_manifest.get("target")]
                + [record.get("target") for record in records]
            ),
            "mode": _first_present(
                [round_manifest.get("mode")]
                + [record.get("mode") for record in records]
            ),
            "summary": {
                "record_count": len(records),
                "failed_record_count": len(failed_records),
                "connector_count": len(connector_stats),
                "module_count": len(module_stats),
                "case_count": len(case_ids),
                "total_duration_ms": round(
                    sum(float(item.get("duration_ms") or 0.0) for item in records),
                    6,
                ),
                "monitor_event_count": monitoring.get("event_count"),
            },
            "connectors": connector_stats,
            "modules": module_stats,
            "failure_clusters": failure_clusters,
            "slowest_records": [_record_summary(item) for item in slowest],
            "failed_records": [_record_summary(item) for item in failed_records[:20]],
            "case_summary": _case_summary(case_records),
            "coverage": _mapping(round_manifest.get("coverage")),
            "feedback": _mapping(round_manifest.get("feedback")),
            "manifest_artifacts": _mapping(round_manifest.get("artifacts")),
            "campaign": _campaign_summary(campaign_manifest),
            "optimization_hints": _optimization_hints(
                connector_stats,
                module_stats,
                failure_clusters,
                slowest,
            ),
        }

    def _llm_dataset(
        self,
        records: list[dict[str, Any]],
        evaluation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ranked = _rank_records_for_llm(records)
        artifacts = _mapping(evaluation.get("manifest_artifacts"))
        items = []
        for record in ranked[: max(0, self.max_llm_records)]:
            items.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "libafl_bfm_fuzz.llm_harness_optimization_sample",
                    "task": "harness_module_diagnosis",
                    "target": evaluation.get("target"),
                    "mode": evaluation.get("mode"),
                    "run_id": record.get("run_id") or evaluation.get("run_id"),
                    "round_id": record.get("round_id") or evaluation.get("round_id"),
                    "case_index": record.get("case_index"),
                    "connector": record.get("connector"),
                    "step": record.get("step"),
                    "from_layer": record.get("from_layer"),
                    "to_layer": record.get("to_layer"),
                    "signals": {
                        "status": record.get("status"),
                        "duration_ms": record.get("duration_ms"),
                        "metrics": record.get("metrics", {}),
                        "error": record.get("error"),
                    },
                    "evidence": {
                        **_mapping(record.get("evidence")),
                        "artifacts": artifacts,
                    },
                    "objective": (
                        "Use the observed harness boundary data to improve "
                        "stimulus generation, replay, reference modeling, "
                        "scoreboard checks, or coverage feedback without "
                        "changing unrelated behavior."
                    ),
                }
            )
        return items


def _iter_jsonl_objects(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_no, value


def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _connector_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("connector", ""))].append(record)
    return [
        _group_stats(name, items, key_name="connector")
        for name, items in sorted(grouped.items())
    ]


def _module_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        module = str(record.get("to_layer") or record.get("connector") or "")
        grouped[module].append(record)
    return [
        _group_stats(name, items, key_name="module")
        for name, items in sorted(grouped.items())
    ]


def _group_stats(
    name: str,
    items: list[dict[str, Any]],
    *,
    key_name: str,
) -> dict[str, Any]:
    durations = [float(item.get("duration_ms") or 0.0) for item in items]
    failed = [item for item in items if item.get("status") != "ok"]
    return {
        key_name: name,
        "count": len(items),
        "failed": len(failed),
        "ok": len(items) - len(failed),
        "total_duration_ms": round(sum(durations), 6),
        "avg_duration_ms": round(sum(durations) / len(durations), 6) if durations else 0.0,
        "max_duration_ms": round(max(durations), 6) if durations else 0.0,
        "from_layers": sorted({str(item.get("from_layer", "")) for item in items}),
        "to_layers": sorted({str(item.get("to_layer", "")) for item in items}),
    }


def _failure_clusters(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("status") == "ok":
            continue
        error = _mapping(record.get("error"))
        key = (
            str(record.get("connector", "")),
            str(error.get("type", "")),
            str(error.get("message", "")),
        )
        grouped[key].append(record)
    clusters = []
    for (connector, error_type, message), items in grouped.items():
        clusters.append(
            {
                "connector": connector,
                "error_type": error_type,
                "message": message,
                "count": len(items),
                "examples": [_record_summary(item) for item in items[:5]],
            }
        )
    return sorted(clusters, key=lambda item: (-int(item["count"]), item["connector"]))


def _case_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        index = record.get("case_index")
        if isinstance(index, int):
            grouped[index].append(record)
    summary = []
    for index, items in sorted(grouped.items()):
        failed = [item for item in items if item.get("status") != "ok"]
        summary.append(
            {
                "case_index": index,
                "record_count": len(items),
                "failed_record_count": len(failed),
                "connectors": sorted({str(item.get("connector", "")) for item in items}),
                "failed_connectors": sorted(
                    {str(item.get("connector", "")) for item in failed}
                ),
            }
        )
    return summary


def _campaign_summary(campaign_manifest: dict[str, Any]) -> dict[str, Any]:
    modes = campaign_manifest.get("modes", [])
    mode_items = modes if isinstance(modes, list) else []
    rounds = [
        item
        for mode in mode_items
        if isinstance(mode, dict)
        for item in mode.get("rounds", [])
        if isinstance(item, dict)
    ]
    return {
        "mode_count": len(mode_items),
        "round_count": len(rounds),
        "modes": [
            str(mode.get("mode"))
            for mode in mode_items
            if isinstance(mode, dict) and mode.get("mode") is not None
        ],
    }


def _optimization_hints(
    connector_stats: list[dict[str, Any]],
    module_stats: list[dict[str, Any]],
    failure_clusters: list[dict[str, Any]],
    slowest: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "failing_connectors": [
            item["connector"] for item in connector_stats if int(item.get("failed", 0)) > 0
        ],
        "failing_modules": [
            item["module"] for item in module_stats if int(item.get("failed", 0)) > 0
        ],
        "top_failure_clusters": failure_clusters[:5],
        "slow_connectors": [
            _record_summary(item)
            for item in slowest
            if float(item.get("duration_ms") or 0.0) > 0.0
        ][:5],
    }


def _rank_records_for_llm(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            0 if item.get("status") != "ok" else 1,
            -float(item.get("duration_ms") or 0.0),
            str(item.get("connector", "")),
        ),
    )


def _record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector": record.get("connector"),
        "step": record.get("step"),
        "from_layer": record.get("from_layer"),
        "to_layer": record.get("to_layer"),
        "status": record.get("status"),
        "duration_ms": record.get("duration_ms"),
        "case_index": record.get("case_index"),
        "error": record.get("error"),
        "evidence": record.get("evidence"),
    }


def _status(event: dict[str, Any]) -> str:
    status = event.get("status")
    if isinstance(status, str) and status:
        return status
    return "failed" if event.get("event_type") == "connector.failed" else "ok"


def _case_index(metadata: dict[str, Any]) -> int | None:
    for key in ("case_index", "index"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def _first_present(values: Iterable[Any]) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _optional_path(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, int | float) else None
