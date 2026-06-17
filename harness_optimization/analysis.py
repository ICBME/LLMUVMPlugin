from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .records import (
    case_key,
    first_present,
    mapping,
    optional_path,
    record_summary,
)


HARNESS_EVALUATION_KIND = "harness_optimization.harness_evaluation"


class HarnessAnalyzerProtocol(Protocol):
    def analyze(
        self,
        records: list[dict[str, Any]],
        *,
        observation_events: Path,
        monitoring_path: Path | None,
        round_manifest_path: Path | None,
        campaign_manifest_path: Path | None,
        monitoring: dict[str, Any],
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
        trace_quality: dict[str, Any],
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class HarnessEvaluationAnalyzer:
    kind: str = HARNESS_EVALUATION_KIND

    def analyze(
        self,
        records: list[dict[str, Any]],
        *,
        observation_events: Path,
        monitoring_path: Path | None,
        round_manifest_path: Path | None,
        campaign_manifest_path: Path | None,
        monitoring: dict[str, Any],
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
        trace_quality: dict[str, Any],
    ) -> dict[str, Any]:
        connector_stats = connector_stats_for(records)
        module_stats = module_stats_for(records)
        failure_clusters = failure_clusters_for(records)
        slowest = sorted(
            records,
            key=lambda item: float(item.get("duration_ms") or 0.0),
            reverse=True,
        )[:10]
        failed_records = [item for item in records if item.get("status") != "ok"]
        case_records = [item for item in records if case_key(item) is not None]
        case_keys = sorted(
            str(key) for key in {case_key(item) for item in case_records} if key is not None
        )
        directive_records = [
            item for item in records if item.get("directive_id") is not None
        ]
        return {
            "schema_version": 1,
            "kind": self.kind,
            "sources": {
                "observation_events": str(observation_events),
                **optional_path("monitoring", monitoring_path),
                **optional_path("round_manifest", round_manifest_path),
                **optional_path("campaign_manifest", campaign_manifest_path),
            },
            "run_id": first_present(
                [round_manifest.get("run_id"), campaign_manifest.get("run_id")]
                + [record.get("run_id") for record in records]
            ),
            "round_id": first_present(
                [round_manifest.get("round_id")]
                + [record.get("round_id") for record in records]
            ),
            "target": first_present(
                [round_manifest.get("target"), campaign_manifest.get("target")]
                + [record.get("target") for record in records]
            ),
            "mode": first_present(
                [round_manifest.get("mode")]
                + [record.get("mode") for record in records]
            ),
            "summary": {
                "record_count": len(records),
                "failed_record_count": len(failed_records),
                "connector_count": len(connector_stats),
                "module_count": len(module_stats),
                "case_count": len(case_keys),
                "directive_count": len(
                    {str(item.get("directive_id")) for item in directive_records}
                ),
                "hanging_span_count": trace_quality.get("hanging_span_count", 0),
                "orphan_final_count": trace_quality.get("orphan_final_count", 0),
                "missing_span_id_count": trace_quality.get("missing_span_id_count", 0),
                "malformed_event_line_count": trace_quality.get(
                    "malformed_line_count",
                    0,
                ),
                "total_duration_ms": round(
                    sum(float(item.get("duration_ms") or 0.0) for item in records),
                    6,
                ),
                "monitor_event_count": monitoring.get("event_count"),
            },
            "connectors": connector_stats,
            "modules": module_stats,
            "failure_clusters": failure_clusters,
            "slowest_records": [record_summary(item) for item in slowest],
            "failed_records": [record_summary(item) for item in failed_records[:20]],
            "case_summary": case_summary_for(case_records),
            "directive_summary": directive_summary_for(directive_records),
            "trace_quality": trace_quality,
            "coverage": mapping(round_manifest.get("coverage")),
            "feedback": mapping(round_manifest.get("feedback")),
            "manifest_artifacts": mapping(round_manifest.get("artifacts")),
            "campaign": campaign_summary_for(campaign_manifest),
            "optimization_hints": optimization_hints_for(
                connector_stats,
                module_stats,
                failure_clusters,
                slowest,
            ),
        }


def connector_stats_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("connector", ""))].append(record)
    return [
        group_stats(name, items, key_name="connector")
        for name, items in sorted(grouped.items())
    ]


def module_stats_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        module = str(record.get("to_layer") or record.get("connector") or "")
        grouped[module].append(record)
    return [
        group_stats(name, items, key_name="module")
        for name, items in sorted(grouped.items())
    ]


def group_stats(
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


def failure_clusters_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("status") == "ok":
            continue
        error = mapping(record.get("error"))
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
                "examples": [record_summary(item) for item in items[:5]],
            }
        )
    return sorted(clusters, key=lambda item: (-int(item["count"]), item["connector"]))


def case_summary_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = case_key(record)
        if key is not None:
            grouped[str(key)].append(record)
    summary = []
    for key, items in sorted(grouped.items()):
        failed = [item for item in items if item.get("status") != "ok"]
        case_ids = sorted(
            {
                str(item.get("case_id"))
                for item in items
                if item.get("case_id") is not None
            }
        )
        case_indexes = sorted(
            {
                int(item["case_index"])
                for item in items
                if isinstance(item.get("case_index"), int)
            }
        )
        summary.append(
            {
                "case_key": key,
                "case_id": case_ids[0] if len(case_ids) == 1 else None,
                "case_ids": case_ids,
                "case_index": case_indexes[0] if len(case_indexes) == 1 else None,
                "case_indexes": case_indexes,
                "directive_ids": sorted(
                    {
                        str(item.get("directive_id"))
                        for item in items
                        if item.get("directive_id") is not None
                    }
                ),
                "corpus_sha256": first_present(
                    item.get("corpus_sha256") for item in items
                ),
                "record_count": len(items),
                "failed_record_count": len(failed),
                "connectors": sorted({str(item.get("connector", "")) for item in items}),
                "failed_connectors": sorted(
                    {str(item.get("connector", "")) for item in failed}
                ),
            }
        )
    return summary


def directive_summary_for(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        directive = record.get("directive_id")
        if directive is not None:
            grouped[str(directive)].append(record)
    summary = []
    for directive, items in sorted(grouped.items()):
        failed = [item for item in items if item.get("status") != "ok"]
        case_keys = sorted(
            str(key) for key in {case_key(item) for item in items} if key is not None
        )
        failed_case_keys = sorted(
            str(key) for key in {case_key(item) for item in failed} if key is not None
        )
        summary.append(
            {
                "directive_id": directive,
                "record_count": len(items),
                "failed_record_count": len(failed),
                "case_count": len(case_keys),
                "failed_case_count": len(failed_case_keys),
                "case_keys": case_keys,
                "failed_case_keys": failed_case_keys,
                "connectors": sorted({str(item.get("connector", "")) for item in items}),
                "failed_connectors": sorted(
                    {str(item.get("connector", "")) for item in failed}
                ),
            }
        )
    return summary


def campaign_summary_for(campaign_manifest: dict[str, Any]) -> dict[str, Any]:
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


def optimization_hints_for(
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
            record_summary(item)
            for item in slowest
            if float(item.get("duration_ms") or 0.0) > 0.0
        ][:5],
    }


__all__ = [
    "HARNESS_EVALUATION_KIND",
    "HarnessAnalyzerProtocol",
    "HarnessEvaluationAnalyzer",
    "campaign_summary_for",
    "case_summary_for",
    "connector_stats_for",
    "directive_summary_for",
    "failure_clusters_for",
    "group_stats",
    "module_stats_for",
    "optimization_hints_for",
]
