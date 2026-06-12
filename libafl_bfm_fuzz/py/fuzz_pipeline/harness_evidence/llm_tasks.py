from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .records import mapping


class HarnessLlmDatasetBuilderProtocol(Protocol):
    def build(
        self,
        records: list[dict[str, Any]],
        evaluation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class HarnessLlmDatasetBuilder:
    max_records: int = 200

    def build(
        self,
        records: list[dict[str, Any]],
        evaluation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ranked = rank_records_for_llm(records)
        artifacts = mapping(evaluation.get("manifest_artifacts"))
        items = []
        for record in ranked[: max(0, self.max_records)]:
            items.append(
                {
                    "schema_version": 1,
                    "kind": "libafl_bfm_fuzz.llm_harness_optimization_sample",
                    "task": "harness_module_diagnosis",
                    "target": evaluation.get("target"),
                    "mode": evaluation.get("mode"),
                    "run_id": record.get("run_id") or evaluation.get("run_id"),
                    "round_id": record.get("round_id") or evaluation.get("round_id"),
                    "case_index": record.get("case_index"),
                    "case_id": record.get("case_id"),
                    "directive_id": record.get("directive_id"),
                    "corpus_sha256": record.get("corpus_sha256"),
                    "span_id": record.get("span_id"),
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
                        **mapping(record.get("evidence")),
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


def rank_records_for_llm(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            0 if item.get("status") != "ok" else 1,
            -float(item.get("duration_ms") or 0.0),
            str(item.get("connector", "")),
        ),
    )
