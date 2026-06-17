from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_optimization.rollup import (
    CampaignTraceRollupBuilder as SharedCampaignTraceRollupBuilder,
    campaign_trace_rollup_path as _campaign_trace_rollup_path,
)


CAMPAIGN_TRACE_ROLLUP_KIND = "libafl_bfm_fuzz.campaign_trace_rollup"


@dataclass(frozen=True)
class CampaignTraceRollupBuilder:
    records: list[dict[str, Any]]
    evaluation: dict[str, Any]
    campaign_manifest: dict[str, Any]

    def build(self) -> dict[str, Any]:
        return SharedCampaignTraceRollupBuilder(
            records=self.records,
            evaluation=self.evaluation,
            campaign_manifest=self.campaign_manifest,
            kind=CAMPAIGN_TRACE_ROLLUP_KIND,
        ).build()

    def write(self, path: Path) -> dict[str, Any]:
        return SharedCampaignTraceRollupBuilder(
            records=self.records,
            evaluation=self.evaluation,
            campaign_manifest=self.campaign_manifest,
            kind=CAMPAIGN_TRACE_ROLLUP_KIND,
        ).write(path)


def campaign_trace_rollup_path(evaluation_path: Path) -> Path:
    return _campaign_trace_rollup_path(evaluation_path)


__all__ = [
    "CAMPAIGN_TRACE_ROLLUP_KIND",
    "CampaignTraceRollupBuilder",
    "campaign_trace_rollup_path",
]
