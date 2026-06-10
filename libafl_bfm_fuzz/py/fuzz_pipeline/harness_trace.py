from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from connector_observe.trace import (
    FINAL_EVENT_TYPES,
    read_json_object,
    read_jsonl_events,
    trace_quality,
)

from .harness_analysis import HarnessAnalyzer, UvmFuzzHarnessAnalyzer
from .harness_llm_tasks import (
    HarnessLlmDatasetBuilder,
    HarnessLlmDatasetBuilderProtocol,
)
from .harness_metadata import HarnessMetadataExtractorProtocol, UvmFuzzMetadataExtractor
from .harness_records import HarnessRecordProjector, HarnessRecordProjectorProtocol


SCHEMA_VERSION = 1


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
    ignored_hanging_connectors: tuple[str, ...] = ()
    max_llm_records: int = 200
    metadata_extractor: HarnessMetadataExtractorProtocol | None = None
    record_projector: HarnessRecordProjectorProtocol | None = None
    analyzer: HarnessAnalyzer | None = None
    llm_dataset_builder: HarnessLlmDatasetBuilderProtocol | None = None

    def build(self) -> HarnessTraceResult:
        round_manifest = read_json_object(self.round_manifest)
        campaign_manifest = read_json_object(self.campaign_manifest)
        monitoring = read_json_object(self.monitoring)
        events, malformed_line_count = read_jsonl_events(self.observation_events)
        final_events = [
            (line_no, event)
            for line_no, event in events
            if event.get("event_type") in FINAL_EVENT_TYPES
        ]
        records = self._record_projector().project(
            final_events,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
        )
        quality = trace_quality(
            events,
            malformed_line_count,
            ignored_hanging_connectors=set(self.ignored_hanging_connectors),
        )
        evaluation = self._analyzer().analyze(
            records,
            observation_events=self.observation_events,
            monitoring_path=self.monitoring,
            round_manifest_path=self.round_manifest,
            campaign_manifest_path=self.campaign_manifest,
            monitoring=monitoring,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
            trace_quality=quality,
        )
        llm_dataset = self._llm_dataset_builder().build(records, evaluation)
        return HarnessTraceResult(
            records=records,
            evaluation=evaluation,
            llm_dataset=llm_dataset,
        )

    def write(self, outputs: HarnessTraceOutputs) -> HarnessTraceResult:
        result = self.build()
        outputs.records.parent.mkdir(parents=True, exist_ok=True)
        outputs.records.write_text(
            "".join(
                json.dumps(record, sort_keys=True) + "\n"
                for record in result.records
            ),
            encoding="utf-8",
        )
        outputs.evaluation.parent.mkdir(parents=True, exist_ok=True)
        outputs.evaluation.write_text(
            json.dumps(result.evaluation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        outputs.llm_dataset.parent.mkdir(parents=True, exist_ok=True)
        outputs.llm_dataset.write_text(
            "".join(
                json.dumps(item, sort_keys=True) + "\n"
                for item in result.llm_dataset
            ),
            encoding="utf-8",
        )
        return result

    def _record_projector(self) -> HarnessRecordProjectorProtocol:
        return self.record_projector or HarnessRecordProjector(
            observation_events=self.observation_events,
            monitoring=self.monitoring,
            round_manifest=self.round_manifest,
            campaign_manifest=self.campaign_manifest,
            metadata_extractor=self.metadata_extractor or UvmFuzzMetadataExtractor(),
        )

    def _analyzer(self) -> HarnessAnalyzer:
        return self.analyzer or UvmFuzzHarnessAnalyzer()

    def _llm_dataset_builder(self) -> HarnessLlmDatasetBuilderProtocol:
        return self.llm_dataset_builder or HarnessLlmDatasetBuilder(
            max_records=self.max_llm_records,
        )
