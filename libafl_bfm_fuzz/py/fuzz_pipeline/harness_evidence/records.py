from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from harness_optimization.metadata import HarnessMetadataExtractorProtocol
from harness_optimization.records import (
    HarnessExecutionRecordProjector as SharedHarnessExecutionRecordProjector,
    HarnessRecordProjectorProtocol,
    case_key,
    first_present,
    list_value,
    mapping,
    number,
    optional_path,
    record_summary,
)

from .metadata import UvmFuzzMetadataExtractor


HARNESS_EXECUTION_RECORD_KIND = "libafl_bfm_fuzz.harness_execution_record"


@dataclass(frozen=True)
class HarnessRecordProjector:
    observation_events: Path
    monitoring: Path | None = None
    round_manifest: Path | None = None
    campaign_manifest: Path | None = None
    metadata_extractor: HarnessMetadataExtractorProtocol = UvmFuzzMetadataExtractor()
    kind: str = HARNESS_EXECUTION_RECORD_KIND

    def project(
        self,
        events: Iterable[tuple[int, dict[str, Any]]],
        *,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return self._shared_projector().project(
            events,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
        )

    def record_from_event(
        self,
        event: dict[str, Any],
        *,
        line_no: int,
        round_manifest: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return self._shared_projector().record_from_event(
            event,
            line_no=line_no,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
        )

    def _shared_projector(self) -> SharedHarnessExecutionRecordProjector:
        return SharedHarnessExecutionRecordProjector(
            observation_events=self.observation_events,
            metadata_extractor=self.metadata_extractor,
            monitoring=self.monitoring,
            round_manifest=self.round_manifest,
            campaign_manifest=self.campaign_manifest,
            kind=self.kind,
        )


__all__ = [
    "HARNESS_EXECUTION_RECORD_KIND",
    "HarnessRecordProjector",
    "HarnessRecordProjectorProtocol",
    "case_key",
    "first_present",
    "list_value",
    "mapping",
    "number",
    "optional_path",
    "record_summary",
]
