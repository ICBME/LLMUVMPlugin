from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_optimization.analysis import (
    HarnessAnalyzerProtocol,
    HarnessEvaluationAnalyzer as SharedHarnessEvaluationAnalyzer,
    campaign_summary_for,
    case_summary_for,
    connector_stats_for,
    directive_summary_for,
    failure_clusters_for,
    group_stats,
    module_stats_for,
    optimization_hints_for,
)


HarnessAnalyzer = HarnessAnalyzerProtocol
HARNESS_EVALUATION_KIND = "libafl_bfm_fuzz.harness_evaluation"


@dataclass(frozen=True)
class UvmFuzzHarnessAnalyzer:
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
        return SharedHarnessEvaluationAnalyzer(kind=self.kind).analyze(
            records,
            observation_events=observation_events,
            monitoring_path=monitoring_path,
            round_manifest_path=round_manifest_path,
            campaign_manifest_path=campaign_manifest_path,
            monitoring=monitoring,
            round_manifest=round_manifest,
            campaign_manifest=campaign_manifest,
            trace_quality=trace_quality,
        )


__all__ = [
    "HARNESS_EVALUATION_KIND",
    "HarnessAnalyzer",
    "UvmFuzzHarnessAnalyzer",
    "campaign_summary_for",
    "case_summary_for",
    "connector_stats_for",
    "directive_summary_for",
    "failure_clusters_for",
    "group_stats",
    "module_stats_for",
    "optimization_hints_for",
]
