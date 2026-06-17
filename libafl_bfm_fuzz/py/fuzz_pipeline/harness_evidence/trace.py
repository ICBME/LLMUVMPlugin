from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness_optimization.trace import (
    HarnessTraceBuilder as SharedHarnessTraceBuilder,
    HarnessTraceOutputs,
    HarnessTraceResult,
)

from .analysis import HarnessAnalyzer, UvmFuzzHarnessAnalyzer
from .llm_tasks import (
    HarnessLlmDatasetBuilder,
    HarnessLlmDatasetBuilderProtocol,
)
from .metadata import HarnessMetadataExtractorProtocol, UvmFuzzMetadataExtractor
from .records import HarnessRecordProjector, HarnessRecordProjectorProtocol

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
        return self._shared_builder().build()

    def write(self, outputs: HarnessTraceOutputs) -> HarnessTraceResult:
        return self._shared_builder().write(outputs)

    def _shared_builder(self) -> SharedHarnessTraceBuilder:
        return SharedHarnessTraceBuilder(
            observation_events=self.observation_events,
            record_projector=self._record_projector(),
            analyzer=self._analyzer(),
            llm_dataset_builder=self._llm_dataset_builder(),
            monitoring=self.monitoring,
            round_manifest=self.round_manifest,
            campaign_manifest=self.campaign_manifest,
            ignored_hanging_connectors=self.ignored_hanging_connectors,
        )

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


__all__ = [
    "HarnessTraceBuilder",
    "HarnessTraceOutputs",
    "HarnessTraceResult",
]
