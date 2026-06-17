from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness_optimization.evaluation import (
    CampaignEvaluationAdapter as SharedCampaignEvaluationAdapter,
    CampaignEvaluationBackend,
    CampaignRollupAttachment,
    EvaluationConfigView,
    RoundEvaluationBackend,
    RunEvaluationAdapter as SharedRunEvaluationAdapter,
)
from harness_optimization.observation import ObservationContext
from harness_optimization.optimization import (
    HarnessCandidateEvaluationBackend,
    HarnessOptimizerBackend,
)
from harness_optimization.paths import RunPathResolver
from harness_optimization.plugins import HarnessPluginRegistry

from .coverage_feedback import CoverageFeedbackResult
from .harness_evidence.trace import HarnessTraceBuilder
from .harness_evidence.rollup import CampaignTraceRollupBuilder, campaign_trace_rollup_path


@dataclass(frozen=True)
class EvaluationBackends:
    round_evaluation: RoundEvaluationBackend | None = None
    campaign_evaluation: CampaignEvaluationBackend | None = None
    harness_optimizer: HarnessOptimizerBackend | None = None
    harness_candidate_evaluation: HarnessCandidateEvaluationBackend | None = None
    harness_plugin_registry: HarnessPluginRegistry | None = None


@dataclass(frozen=True)
class RunEvaluationAdapter:
    config: EvaluationConfigView
    paths: RunPathResolver
    observation_context: ObservationContext
    round_id: Callable[[], str | None]

    def run_round(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, Any]:
        return self._shared().run_round(stage_results)

    def round_payload(self, stage_results: dict[str, object]) -> dict[str, Any]:
        return self._shared().round_payload(stage_results)

    def _shared(self) -> SharedRunEvaluationAdapter:
        return SharedRunEvaluationAdapter(
            config=self.config,
            paths=self.paths,
            observation_context=self.observation_context,
            round_id=self.round_id,
            trace_builder_factory=_build_round_trace_builder,
            feedback_snapshot_builder=_feedback_snapshot,
            kind="libafl_bfm_fuzz.round_evaluation",
        )


@dataclass(frozen=True)
class CampaignEvaluationAdapter:
    target: str
    path: Path
    observation_context: ObservationContext
    cwd: Path

    def run(
        self,
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        return self._shared().run(campaign_manifest)

    def payload(self, campaign_manifest: dict[str, Any]) -> dict[str, Any]:
        return self._shared().payload(campaign_manifest)

    def _shared(self) -> SharedCampaignEvaluationAdapter:
        return SharedCampaignEvaluationAdapter(
            target=self.target,
            path=self.path,
            observation_context=self.observation_context,
            cwd=self.cwd,
            trace_builder_factory=_build_campaign_trace_builder,
            rollup_writer=_write_campaign_rollup,
            kind="libafl_bfm_fuzz.campaign_evaluation",
        )


def _build_round_trace_builder(
    *,
    observation_events: Path,
    monitoring: Path | None,
    round_manifest: Path | None,
    ignored_hanging_connectors: tuple[str, ...],
) -> HarnessTraceBuilder:
    return HarnessTraceBuilder(
        observation_events=observation_events,
        monitoring=monitoring,
        round_manifest=round_manifest,
        ignored_hanging_connectors=ignored_hanging_connectors,
    )


def _build_campaign_trace_builder(
    *,
    observation_events: Path,
    monitoring: Path | None,
    campaign_manifest: Path | None,
    ignored_hanging_connectors: tuple[str, ...],
) -> HarnessTraceBuilder:
    return HarnessTraceBuilder(
        observation_events=observation_events,
        monitoring=monitoring,
        campaign_manifest=campaign_manifest,
        ignored_hanging_connectors=ignored_hanging_connectors,
    )


def _feedback_snapshot(result: object) -> dict[str, Any]:
    if not isinstance(result, CoverageFeedbackResult):
        return {}
    directives = result.final_directives.get("directives", [])
    return {
        "directive_count": len(directives),
        "directive_source": result.final_directives.get("source"),
        "has_gap_feedback": result.gap_feedback is not None,
        "has_mutation_feedback": result.mutation_feedback is not None,
    }


def _write_campaign_rollup(
    result,
    campaign_manifest: dict[str, Any],
    evaluation_path: Path,
) -> CampaignRollupAttachment:
    rollup_path = campaign_trace_rollup_path(evaluation_path)
    payload = CampaignTraceRollupBuilder(
        records=result.records,
        evaluation=result.evaluation,
        campaign_manifest=campaign_manifest,
    ).write(rollup_path)
    return CampaignRollupAttachment(path=rollup_path, payload=payload)
