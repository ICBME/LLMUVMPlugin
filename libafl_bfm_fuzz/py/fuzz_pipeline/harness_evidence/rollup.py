from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from .records import case_key, first_present, mapping


@dataclass(frozen=True)
class CampaignTraceRollupBuilder:
    records: list[dict[str, Any]]
    evaluation: dict[str, Any]
    campaign_manifest: dict[str, Any]

    def build(self) -> dict[str, Any]:
        rounds = _round_items(self.campaign_manifest)
        round_summaries = _round_summaries(self.records, rounds)
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.campaign_trace_rollup",
            "run_id": self.evaluation.get("run_id")
            or self.campaign_manifest.get("run_id"),
            "target": self.evaluation.get("target")
            or self.campaign_manifest.get("target"),
            "summary": {
                "mode_count": len(
                    {
                        str(item.get("mode"))
                        for item in rounds
                        if item.get("mode") is not None
                    }
                ),
                "round_count": len(rounds),
                "record_count": len(self.records),
                "failed_record_count": len(
                    [item for item in self.records if item.get("status") != "ok"]
                ),
                "case_count": len(
                    {
                        str(key)
                        for key in {_case_key(item) for item in self.records}
                        if key is not None
                    }
                ),
                "directive_count": len(
                    {
                        str(item.get("directive_id"))
                        for item in self.records
                        if item.get("directive_id") is not None
                    }
                ),
                "coverage_metric_count": len(_coverage_metric_names(rounds)),
            },
            "rounds": round_summaries,
            "coverage_trends": _coverage_trends(round_summaries),
            "directive_effectiveness": _directive_effectiveness(self.records),
            "case_effectiveness": _case_effectiveness(self.records),
            "failure_trends": _failure_trends(self.records, round_summaries),
        }

    def write(self, path: Path) -> dict[str, Any]:
        payload = self.build()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload


def campaign_trace_rollup_path(evaluation_path: Path) -> Path:
    return evaluation_path.with_name(f"{evaluation_path.stem}_campaign_rollup.json")


def _round_items(campaign_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    modes = campaign_manifest.get("modes", [])
    mode_items = modes if isinstance(modes, list) else []
    for mode_item in mode_items:
        if not isinstance(mode_item, dict):
            continue
        mode = mode_item.get("mode")
        rounds = mode_item.get("rounds", [])
        round_items = rounds if isinstance(rounds, list) else []
        for index, item in enumerate(round_items):
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "mode": mode,
                    "round_index": item.get("index", index),
                    "round_id": item.get("round_id"),
                    "round_manifest": item.get("round_manifest"),
                    "coverage": mapping(item.get("coverage")),
                    "feedback": mapping(item.get("feedback")),
                }
            )
    return result


def _round_summaries(
    records: list[dict[str, Any]],
    rounds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records_by_round: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        round_id = record.get("round_id")
        if round_id is not None:
            records_by_round[str(round_id)].append(record)

    previous_by_mode: dict[str, dict[str, Any]] = {}
    summaries = []
    for item in rounds:
        mode = str(item.get("mode") or "")
        round_id = item.get("round_id")
        round_records = records_by_round.get(str(round_id), [])
        failed = [record for record in round_records if record.get("status") != "ok"]
        coverage = mapping(item.get("coverage"))
        previous_coverage = previous_by_mode.get(mode, {})
        coverage_delta = _numeric_delta(coverage, previous_coverage)
        previous_by_mode[mode] = coverage
        case_keys = {
            str(key)
            for key in {_case_key(record) for record in round_records}
            if key is not None
        }
        directive_ids = {
            str(record.get("directive_id"))
            for record in round_records
            if record.get("directive_id") is not None
        }
        summaries.append(
            {
                "mode": item.get("mode"),
                "round_index": item.get("round_index"),
                "round_id": round_id,
                "round_manifest": item.get("round_manifest"),
                "record_count": len(round_records),
                "failed_record_count": len(failed),
                "case_count": len(case_keys),
                "directive_count": len(directive_ids),
                "coverage": coverage,
                "coverage_delta": coverage_delta,
                "feedback": mapping(item.get("feedback")),
                "failed_connectors": sorted(
                    {str(record.get("connector", "")) for record in failed}
                ),
            }
        )
    return summaries


def _coverage_trends(round_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = sorted(
        {
            key
            for summary in round_summaries
            for key, value in mapping(summary.get("coverage")).items()
            if isinstance(value, int | float)
        }
    )
    trends = []
    for name in names:
        values = []
        for summary in round_summaries:
            coverage = mapping(summary.get("coverage"))
            value = coverage.get(name)
            if not isinstance(value, int | float):
                continue
            values.append(
                {
                    "mode": summary.get("mode"),
                    "round_id": summary.get("round_id"),
                    "round_index": summary.get("round_index"),
                    "value": value,
                    "delta_from_previous": mapping(
                        summary.get("coverage_delta")
                    ).get(name),
                }
            )
        trends.append({"metric": name, "values": values})
    return trends


def _directive_effectiveness(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        directive_id = record.get("directive_id")
        if directive_id is not None:
            grouped[str(directive_id)].append(record)
    result = []
    for directive_id, items in sorted(grouped.items()):
        failed = [item for item in items if item.get("status") != "ok"]
        case_keys = {
            str(key) for key in {_case_key(item) for item in items} if key is not None
        }
        failed_case_keys = {
            str(key) for key in {_case_key(item) for item in failed} if key is not None
        }
        result.append(
            {
                "directive_id": directive_id,
                "record_count": len(items),
                "failed_record_count": len(failed),
                "case_count": len(case_keys),
                "failed_case_count": len(failed_case_keys),
                "round_ids": _sorted_present(item.get("round_id") for item in items),
                "connectors": sorted({str(item.get("connector", "")) for item in items}),
                "failed_connectors": sorted(
                    {str(item.get("connector", "")) for item in failed}
                ),
            }
        )
    return result


def _case_effectiveness(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = _case_key(record)
        if key is not None:
            grouped[str(key)].append(record)
    result = []
    for key, items in sorted(grouped.items()):
        failed = [item for item in items if item.get("status") != "ok"]
        result.append(
            {
                "case_key": key,
                "case_id": first_present(item.get("case_id") for item in items),
                "case_index": first_present(item.get("case_index") for item in items),
                "directive_ids": _sorted_present(
                    item.get("directive_id") for item in items
                ),
                "record_count": len(items),
                "failed_record_count": len(failed),
                "round_ids": _sorted_present(item.get("round_id") for item in items),
                "connectors": sorted({str(item.get("connector", "")) for item in items}),
                "failed_connectors": sorted(
                    {str(item.get("connector", "")) for item in failed}
                ),
            }
        )
    return result


def _failure_trends(
    records: list[dict[str, Any]],
    round_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failures_by_round_connector: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        if record.get("status") == "ok":
            continue
        round_id = record.get("round_id")
        if round_id is None:
            continue
        key = (str(round_id), str(record.get("connector", "")))
        failures_by_round_connector[key] += 1

    result = []
    for summary in round_summaries:
        round_id = summary.get("round_id")
        connectors = [
            {
                "connector": connector,
                "failed_record_count": count,
            }
            for (item_round_id, connector), count in sorted(
                failures_by_round_connector.items()
            )
            if item_round_id == str(round_id)
        ]
        result.append(
            {
                "mode": summary.get("mode"),
                "round_index": summary.get("round_index"),
                "round_id": round_id,
                "failed_record_count": summary.get("failed_record_count", 0),
                "connectors": connectors,
            }
        )
    return result


def _numeric_delta(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, float | int]:
    delta: dict[str, float | int] = {}
    for key, value in current.items():
        previous_value = previous.get(key)
        if isinstance(value, int | float) and isinstance(previous_value, int | float):
            delta[key] = value - previous_value
    return delta


def _coverage_metric_names(rounds: list[dict[str, Any]]) -> set[str]:
    return {
        key
        for item in rounds
        for key, value in mapping(item.get("coverage")).items()
        if isinstance(value, int | float)
    }


def _case_key(record: dict[str, Any]) -> str | int | None:
    return case_key(record)


def _sorted_present(values: Iterable[Any]) -> list[str]:
    return sorted({str(value) for value in values if value is not None})
