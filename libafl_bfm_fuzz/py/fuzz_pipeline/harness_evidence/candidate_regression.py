from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ConnectGraph import ObservationContext
from ConnectGraph.trace import read_json_object
from harness_optimization.candidate_validation import (
    build_action_pruning_summary as _build_action_pruning_summary,
    build_candidate_promotion_package as _build_candidate_promotion_package,
    build_candidate_variant_ranking as _build_candidate_variant_ranking,
    candidate_variant_evaluation_payload,
    combined_variant_evaluation,
    metric_change_summary,
    numeric_variant_metrics,
    paired_metric_stability,
    paired_validation_run_payload,
    select_top_variant_evaluation,
    aggregate_metric_snapshots,
)
from harness_optimization.candidate_execution import (
    CandidateAcceptanceThresholds,
    CandidateActionAdapter,
    CandidateActionAdapterContext,
    CandidateActionAdapterResult,
    CandidateRegressionSettings,
    CandidateRepeatRun,
    MatchedBaselineRun,
    build_candidate_action_overlay as _build_candidate_action_overlay,
    candidate_regression_config_payload as _candidate_regression_config_payload,
    action_entry_for,
    action_variant,
    adapter_artifacts,
    adapter_make_vars,
    adapter_metric_snapshot,
    build_candidate_directives,
    build_candidate_variants,
    directives_from_action_entry,
    filter_candidate_actions_for_variant,
    select_candidate_variants,
)

from ..campaign_orchestrator import CampaignConfig, CampaignOrchestrator
from .optimization import (
    CANDIDATE_EVALUATION_KIND,
    PROPOSAL_KIND,
    baseline_metric_snapshot,
    default_harness_plugin_registry,
    list_value,
    number_value,
    safe_slug,
    utc_timestamp,
    validate_harness_optimization_proposal,
)
from .plugins import (
    HarnessGapActionabilityContext,
    HarnessPluginRegistry,
    plugin_registry_provenance_json,
    plugin_registry_to_json,
    plugin_registry_validation_json,
    require_valid_harness_plugin_registry,
)
from harness_optimization.io import (
    artifact_path as _artifact_path,
    campaign_cwd as _campaign_cwd,
    optional_artifact as _optional_artifact,
    optional_existing_artifact as _optional_existing_artifact,
    path_or_none as _path_or_none,
    required_path as _required_path,
    write_json as _write_json,
)
from ..harness_runtime_actions import (
    RUNTIME_METRICS_OUT_ENV,
    read_runtime_metrics,
    runtime_metric_snapshot,
)
from .records import mapping
from ..run_adapters import RunBackends
from ..run_evaluation import EvaluationBackends
from ..run_orchestrator import FuzzRunOrchestrator
from ..run_profiles import RunPlanProfile
from ..topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


CampaignOrchestratorFactory = Callable[..., CampaignOrchestrator]

CANDIDATE_ACTION_OVERLAY_KIND = (
    "libafl_bfm_fuzz.harness_optimization_candidate_action_overlay"
)
CANDIDATE_REGRESSION_CONFIG_KIND = (
    "libafl_bfm_fuzz.harness_candidate_regression_config"
)
CANDIDATE_VARIANT_RANKING_KIND = "libafl_bfm_fuzz.harness_candidate_variant_ranking"
CANDIDATE_PROMOTION_PACKAGE_KIND = (
    "libafl_bfm_fuzz.harness_candidate_promotion_package"
)
MINIMAL_PROMOTION_CANDIDATE_KIND = (
    "libafl_bfm_fuzz.harness_minimal_promotion_candidate"
)


def build_candidate_action_overlay(
    candidate_manifest: Mapping[str, Any],
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    *,
    kind: str = CANDIDATE_ACTION_OVERLAY_KIND,
) -> dict[str, Any]:
    return _build_candidate_action_overlay(
        candidate_manifest,
        adapter_results,
        kind=kind,
    )


def candidate_regression_config_payload(
    config: CampaignConfig,
    *,
    action_overlay: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    initial_directives: Path | None,
    runtime_metrics: Path,
    run_role: str = "candidate",
    settings: CandidateRegressionSettings | None = None,
    kind: str = CANDIDATE_REGRESSION_CONFIG_KIND,
) -> dict[str, Any]:
    return _candidate_regression_config_payload(
        config,
        action_overlay=action_overlay,
        adapter_results=adapter_results,
        initial_directives=initial_directives,
        runtime_metrics=runtime_metrics,
        run_role=run_role,
        settings=settings,
        kind=kind,
    )


@dataclass(frozen=True)
class JsonConfigActionAdapter:
    action_type: str
    plugin_registry: HarnessPluginRegistry | None = None

    def adapt(
        self,
        actions: tuple[dict[str, Any], ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        kind, artifact_role, make_var = adapter_config_for(
            self.action_type,
            plugin_registry=self.plugin_registry,
        )
        entries = tuple(action_entry_for(action) for action in actions)
        artifact = {
            "schema_version": 1,
            "kind": kind,
            "created_at": utc_timestamp(),
            "candidate_id": context.candidate_id,
            "action_type": self.action_type,
            "entries": list(entries),
        }
        artifact_path = context.regression_dir / f"{artifact_role}.json"
        _write_json(artifact_path, artifact)
        directives = tuple(
            directive
            for entry in entries
            for directive in directives_from_action_entry(entry)
        )
        return CandidateActionAdapterResult(
            action_type=self.action_type,
            artifact_role=artifact_role,
            artifact_path=artifact_path,
            make_var=make_var,
            entries=entries,
            directives=directives,
            variants=tuple(
                action_variant(entry, artifact_path=artifact_path)
                for entry in entries
            ),
            metric_counts={f"{self.action_type}_count": len(entries)},
        )


@dataclass(frozen=True)
class HarnessCandidateRegressionBackend:
    settings: CandidateRegressionSettings = CandidateRegressionSettings()
    topology: PipelineTopology = FULL_FUZZ_TOPOLOGY
    run_backends: RunBackends | None = None
    run_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    campaign_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    evaluation_backends: EvaluationBackends | None = None
    action_adapters: Mapping[str, CandidateActionAdapter] | None = None
    plugin_registry: HarnessPluginRegistry | None = None
    run_orchestrator_factory: Callable[..., FuzzRunOrchestrator] = FuzzRunOrchestrator
    campaign_orchestrator_factory: CampaignOrchestratorFactory = CampaignOrchestrator
    observation_context: ObservationContext | None = None

    def _plugin_registry(self) -> HarnessPluginRegistry:
        return candidate_regression_plugin_registry(self.plugin_registry)

    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        plugin_registry = self._plugin_registry()
        plugin_validation = plugin_registry_validation_json(plugin_registry)
        if self.settings.strict_plugin_validation:
            plugin_validation = require_valid_harness_plugin_registry(
                plugin_registry,
                context="candidate regression plugin registry",
            )
        source_baseline_metrics = baseline_metric_snapshot(task)
        baseline_metrics = source_baseline_metrics
        baseline_source = "task_snapshot"
        matched_baseline: MatchedBaselineRun | None = None
        matched_baseline_summary: dict[str, Any] = {
            "enabled": self.settings.matched_baseline,
            "status": "disabled",
            "baseline_source": baseline_source,
        }
        sandbox_dir = _required_path(candidate_manifest.get("sandbox_dir"))
        regression_dir = sandbox_dir / "candidate_regression"
        regression_dir.mkdir(parents=True, exist_ok=True)
        plugin_provenance_path = regression_dir / "candidate_plugin_provenance.json"
        _write_json(
            plugin_provenance_path,
            plugin_registry_provenance_json(plugin_registry),
        )
        baseline_manifest_path = _required_path(
            mapping(task.get("artifacts")).get("campaign_manifest")
            or mapping(task.get("sources")).get("campaign_manifest")
        )
        baseline_manifest = read_json_object(baseline_manifest_path)
        raw_actions = load_candidate_actions(candidate_manifest)
        adapter_context = CandidateActionAdapterContext(
            candidate_id=str(candidate_manifest.get("candidate_id") or "candidate"),
            regression_dir=regression_dir,
        )
        adapter_results = adapt_candidate_actions(
            raw_actions,
            adapter_context,
            adapters=self.action_adapters,
            plugin_registry=plugin_registry,
        )
        overlay = build_candidate_action_overlay(
            candidate_manifest,
            adapter_results,
            kind=CANDIDATE_ACTION_OVERLAY_KIND,
        )
        overlay_path = regression_dir / "candidate_action_overlay.json"
        _write_json(overlay_path, overlay)
        candidate_directives = build_candidate_directives(adapter_results)
        candidate_directives_path = (
            regression_dir / "candidate_mutation_directives.json"
            if candidate_directives["directives"]
            else None
        )
        if candidate_directives_path is not None:
            _write_json(candidate_directives_path, candidate_directives)
        runtime_metrics_path = regression_dir / "candidate_runtime_metrics.json"

        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=regression_dir,
            action_overlay=overlay_path,
            adapter_results=adapter_results,
            initial_directives=candidate_directives_path,
            runtime_metrics=runtime_metrics_path,
        )
        run_config_path = regression_dir / "candidate_regression_config.json"
        _write_json(
            run_config_path,
            candidate_regression_config_payload(
                campaign_config,
                action_overlay=overlay_path,
                adapter_results=adapter_results,
                initial_directives=candidate_directives_path,
                runtime_metrics=runtime_metrics_path,
                settings=self.settings,
                kind=CANDIDATE_REGRESSION_CONFIG_KIND,
            ),
        )
        if int(mapping(patch.get("summary")).get("applied_action_count", 0)) <= 0:
            return self._not_run_report(
                task=task,
                proposal=proposal,
                patch=patch,
                candidate_manifest=candidate_manifest,
                baseline_metrics=source_baseline_metrics,
                run_config_path=run_config_path,
                overlay_path=overlay_path,
                adapter_results=adapter_results,
                directives_path=candidate_directives_path,
                runtime_metrics_path=runtime_metrics_path,
                plugin_provenance_path=plugin_provenance_path,
                reason="no safe applied actions to validate",
            )

        if self.settings.matched_baseline:
            try:
                matched_baseline = self._run_matched_noop_baseline(
                    task=task,
                    proposal=proposal,
                    patch=patch,
                    candidate_manifest=candidate_manifest,
                    baseline_manifest=baseline_manifest,
                    regression_dir=regression_dir,
                )
            except Exception as exc:  # noqa: BLE001 - failed matched baseline rejects evidence
                return self._error_report(
                    task=task,
                    proposal=proposal,
                    baseline_metrics=source_baseline_metrics,
                    baseline_source="task_snapshot",
                    source_baseline_metrics=source_baseline_metrics,
                    run_config_path=run_config_path,
                    overlay_path=overlay_path,
                    adapter_results=adapter_results,
                    directives_path=candidate_directives_path,
                    runtime_metrics_path=runtime_metrics_path,
                    plugin_provenance_path=plugin_provenance_path,
                    error=exc,
                    error_context="matched_noop_baseline",
                    matched_baseline_summary={
                        "enabled": True,
                        "status": "error",
                        "baseline_source": "task_snapshot",
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    },
                )
            baseline_metrics = matched_baseline.metrics
            baseline_source = "matched_noop_rerun"
            matched_baseline_summary = matched_baseline.summary

        try:
            candidate_campaign_manifest = self._run_candidate_campaign(
                campaign_config,
                task=task,
                proposal=proposal,
                patch=patch,
            )
        except Exception as exc:  # noqa: BLE001 - final decision rejects failed validation
            return self._error_report(
                task=task,
                proposal=proposal,
                baseline_metrics=baseline_metrics,
                baseline_source=baseline_source,
                source_baseline_metrics=source_baseline_metrics,
                run_config_path=run_config_path,
                overlay_path=overlay_path,
                adapter_results=adapter_results,
                directives_path=candidate_directives_path,
                runtime_metrics_path=runtime_metrics_path,
                plugin_provenance_path=plugin_provenance_path,
                error=exc,
                error_context="candidate_campaign",
                matched_baseline_artifacts=(
                    matched_baseline.artifacts if matched_baseline is not None else None
                ),
                matched_baseline_summary=matched_baseline_summary,
            )

        candidate_evaluation = read_json_object(campaign_config.campaign_evaluation_out)
        combined_metrics = candidate_metric_snapshot(
            candidate_campaign_manifest,
            candidate_evaluation,
            cwd=_campaign_cwd(candidate_campaign_manifest, campaign_config.cwd),
        )
        combined_metrics.update(adapter_metric_snapshot(adapter_results))
        combined_metrics.update(runtime_metric_snapshot(runtime_metrics_path))
        candidate_run_artifacts = {
            "candidate_regression_config": str(run_config_path),
            "candidate_action_overlay": str(overlay_path),
            "candidate_plugin_provenance": str(plugin_provenance_path),
            **adapter_artifacts(adapter_results),
            **_optional_artifact(
                "candidate_mutation_directives",
                candidate_directives_path,
            ),
            **_optional_existing_artifact(
                "candidate_runtime_metrics",
                runtime_metrics_path,
            ),
            "candidate_campaign_manifest": str(campaign_config.campaign_manifest_out),
            "candidate_campaign_evaluation": str(
                campaign_config.campaign_evaluation_out
            ),
        }
        matched_baseline_metrics = (
            matched_baseline.metrics if matched_baseline is not None else None
        )
        paired_validation_path: Path | None = None
        paired_validation_summary: dict[str, Any] = {
            "enabled": self.settings.paired_repeats > 1,
            "status": "disabled",
            "requested_repeats": self.settings.paired_repeats,
        }
        if self.settings.paired_repeats > 1:
            if matched_baseline is None:
                paired_validation_summary = {
                    "enabled": False,
                    "status": "skipped",
                    "requested_repeats": self.settings.paired_repeats,
                    "reason": "matched_baseline_required",
                }
            else:
                paired_validation = self._run_paired_repeated_validation(
                    task=task,
                    proposal=proposal,
                    patch=patch,
                    candidate_manifest=candidate_manifest,
                    baseline_manifest=baseline_manifest,
                    raw_actions=raw_actions,
                    regression_dir=regression_dir,
                    repeat0_seed=campaign_config.seed,
                    repeat0_baseline=matched_baseline,
                    repeat0_candidate=CandidateRepeatRun(
                        metrics=combined_metrics,
                        artifacts=candidate_run_artifacts,
                        evaluation=candidate_evaluation,
                    ),
                )
                baseline_metrics = paired_validation["baseline_metrics"]
                matched_baseline_metrics = baseline_metrics
                combined_metrics = paired_validation["candidate_metrics"]
                paired_validation_summary = paired_validation["summary"]
                paired_validation_path = _required_path(
                    paired_validation["artifact_path"]
                )
        combined_variant = combined_variant_evaluation(
            overlay=overlay,
            metrics=combined_metrics,
            baseline_metrics=baseline_metrics,
            campaign_config=campaign_config,
            run_config_path=run_config_path,
            runtime_metrics_path=runtime_metrics_path,
            adapter_results=adapter_results,
            extra_artifacts=_optional_artifact(
                "candidate_paired_validation",
                paired_validation_path,
            ),
        )
        selected_variants = select_candidate_variants(
            overlay.get("variants"),
            max_count=(
                self.settings.attribution_top_k
                or self.settings.max_variant_regressions
            ),
            attribution_mode=self.settings.attribution_mode,
        )
        variant_evaluations = [
            combined_variant,
            *self._run_additional_variant_evaluations(
                selected_variants=selected_variants,
                raw_actions=raw_actions,
                task=task,
                proposal=proposal,
                patch=patch,
                candidate_manifest=candidate_manifest,
                baseline_manifest=baseline_manifest,
                baseline_metrics=baseline_metrics,
                regression_dir=regression_dir,
            ),
        ]
        ranking = build_candidate_variant_ranking(
            action_overlay=overlay,
            baseline_metrics=baseline_metrics,
            candidate_metrics=combined_metrics,
            selected_variant_id="combined",
            variant_evaluations=variant_evaluations,
        )
        ranking_path = regression_dir / "candidate_variant_ranking.json"
        _write_json(ranking_path, ranking)
        selected_variant = select_top_variant_evaluation(
            ranking,
            variant_evaluations,
        )
        candidate_metrics = dict(
            mapping(selected_variant.get("metrics")) or combined_metrics
        )
        candidate_status = str(selected_variant.get("status") or "passed")
        if paired_validation_summary.get("status") == "error":
            candidate_status = "error"
        variant_evaluations_path = regression_dir / "candidate_variant_evaluations.json"
        _write_json(
            variant_evaluations_path,
            {
                "schema_version": 1,
                "kind": "libafl_bfm_fuzz.harness_candidate_variant_evaluations",
                "created_at": utc_timestamp(),
                "candidate_id": candidate_manifest.get("candidate_id"),
                "evaluations": variant_evaluations,
            },
        )
        action_effect_report = build_candidate_action_effect_report(
            task=task,
            proposal=proposal,
            candidate_manifest=candidate_manifest,
            baseline_metrics=baseline_metrics,
            variant_evaluations=variant_evaluations,
            plugin_registry=plugin_registry,
        )
        action_effect_report_path = regression_dir / "candidate_action_effect_report.json"
        _write_json(action_effect_report_path, action_effect_report)
        baseline_gap_manifest = (
            read_json_object(
                _required_path(
                    matched_baseline.artifacts.get("matched_baseline_campaign_manifest")
                )
            )
            if matched_baseline is not None
            else baseline_manifest
        )
        gap_actionability_report = build_candidate_gap_actionability_report(
            task=task,
            proposal=proposal,
            candidate_manifest=candidate_manifest,
            baseline_campaign_manifest=baseline_gap_manifest,
            candidate_campaign_manifest=candidate_campaign_manifest,
            baseline_metrics=baseline_metrics,
            candidate_metrics=candidate_metrics,
            action_effect_report=action_effect_report,
            plugin_registry=plugin_registry,
        )
        gap_actionability_report_path = (
            regression_dir / "candidate_gap_actionability_report.json"
        )
        _write_json(gap_actionability_report_path, gap_actionability_report)
        gap_minimal_proposal = build_gap_actionability_minimal_candidate_proposal(
            task=task,
            proposal=proposal,
            candidate_manifest=candidate_manifest,
            gap_actionability_report=gap_actionability_report,
            plugin_registry=plugin_registry,
        )
        gap_minimal_proposal_path = (
            regression_dir / "candidate_gap_actionability_minimal_proposal.json"
        )
        _write_json(gap_minimal_proposal_path, gap_minimal_proposal)
        promotion = build_candidate_promotion_package(
            task=task,
            proposal=proposal,
            patch=patch,
            candidate_manifest=candidate_manifest,
            candidate_metrics=candidate_metrics,
            baseline_metrics=baseline_metrics,
            ranking=ranking,
            thresholds=self.settings.thresholds,
            stability_summary=paired_validation_summary,
            action_effect_report=action_effect_report,
            gap_actionability_report=gap_actionability_report,
            gap_actionability_minimal_proposal=gap_minimal_proposal,
        )
        promotion_path = regression_dir / "candidate_promotion_package.json"
        _write_json(promotion_path, promotion)
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "source": type(self).__name__,
            "status": candidate_status,
            "application_status": patch.get("status"),
            "baseline_source": baseline_source,
            "baseline_metrics": baseline_metrics,
            "source_baseline_metrics": source_baseline_metrics,
            "matched_baseline_metrics": matched_baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "plugin_validation": plugin_validation,
            "plugin_provenance": plugin_registry_provenance_json(plugin_registry),
            "stability_summary": paired_validation_summary,
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                "candidate_plugin_provenance": str(plugin_provenance_path),
                **(
                    matched_baseline.artifacts
                    if matched_baseline is not None
                    else {}
                ),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    candidate_directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
                **_optional_artifact(
                    "candidate_paired_validation",
                    paired_validation_path,
                ),
                "candidate_variant_ranking": str(ranking_path),
                "candidate_variant_evaluations": str(variant_evaluations_path),
                "candidate_action_effect_report": str(action_effect_report_path),
                "candidate_gap_actionability_report": str(
                    gap_actionability_report_path
                ),
                "candidate_gap_actionability_minimal_proposal": str(
                    gap_minimal_proposal_path
                ),
                "candidate_promotion_package": str(promotion_path),
                "candidate_campaign_manifest": str(
                    campaign_config.campaign_manifest_out
                ),
                "candidate_campaign_evaluation": str(
                    campaign_config.campaign_evaluation_out
                ),
            },
            "summary": {
                "candidate_artifact_count": len(
                    list_value(candidate_manifest.get("candidate_artifacts"))
                ),
                "matched_baseline": matched_baseline_summary,
                "paired_validation": paired_validation_summary,
                "candidate_mode_count": len(
                    candidate_campaign_manifest.get("modes", [])
                ),
                "candidate_round_count": sum(
                    len(mode.get("rounds", []))
                    for mode in list_value(candidate_campaign_manifest.get("modes"))
                    if isinstance(mode, dict)
                ),
                "candidate_variant_count": len(ranking.get("variants", [])),
                "evaluated_variant_count": len(variant_evaluations),
                "selected_variant_id": selected_variant.get("variant_id"),
                "promotion_status": promotion.get("promotion_status"),
            },
        }

    def _run_matched_noop_baseline(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_manifest: dict[str, Any],
        regression_dir: Path,
        matched_dir: Path | None = None,
        seed: int | None = None,
        run_role: str = "matched_noop_baseline",
        repeat_index: int | None = None,
    ) -> MatchedBaselineRun:
        matched_dir = matched_dir or regression_dir / "matched_noop_baseline"
        matched_dir.mkdir(parents=True, exist_ok=True)
        matched_candidate_id = ":".join(
            [
                str(candidate_manifest.get("candidate_id") or "candidate"),
                safe_slug(run_role),
            ]
        )
        overlay = build_candidate_action_overlay(
            {**candidate_manifest, "candidate_id": matched_candidate_id},
            (),
            kind=CANDIDATE_ACTION_OVERLAY_KIND,
        )
        overlay["baseline_role"] = "matched_noop_baseline"
        overlay_path = matched_dir / "matched_baseline_action_overlay.json"
        _write_json(overlay_path, overlay)
        runtime_metrics_path = matched_dir / "matched_baseline_runtime_metrics.json"
        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=matched_dir,
            action_overlay=overlay_path,
            adapter_results=(),
            initial_directives=None,
            runtime_metrics=runtime_metrics_path,
            seed=seed,
        )
        run_config_path = matched_dir / "matched_baseline_regression_config.json"
        config_payload = candidate_regression_config_payload(
            campaign_config,
            action_overlay=overlay_path,
            adapter_results=(),
            initial_directives=None,
            runtime_metrics=runtime_metrics_path,
            run_role=run_role,
            settings=self.settings,
            kind=CANDIDATE_REGRESSION_CONFIG_KIND,
        )
        if repeat_index is not None:
            config_payload["repeat_index"] = repeat_index
        _write_json(run_config_path, config_payload)
        campaign_manifest = self._run_candidate_campaign(
            campaign_config,
            task=task,
            proposal=proposal,
            patch=patch,
        )
        campaign_evaluation = read_json_object(campaign_config.campaign_evaluation_out)
        metrics = candidate_metric_snapshot(
            campaign_manifest,
            campaign_evaluation,
            cwd=_campaign_cwd(campaign_manifest, campaign_config.cwd),
        )
        metrics.update(adapter_metric_snapshot(()))
        metrics.update(runtime_metric_snapshot(runtime_metrics_path))
        artifacts = {
            "matched_baseline_regression_config": str(run_config_path),
            "matched_baseline_action_overlay": str(overlay_path),
            "matched_baseline_campaign_manifest": str(
                campaign_config.campaign_manifest_out
            ),
            "matched_baseline_campaign_evaluation": str(
                campaign_config.campaign_evaluation_out
            ),
            **_optional_existing_artifact(
                "matched_baseline_runtime_metrics",
                runtime_metrics_path,
            ),
        }
        return MatchedBaselineRun(
            metrics=metrics,
            artifacts=artifacts,
            summary={
                "enabled": True,
                "status": "passed",
                "baseline_source": "matched_noop_rerun",
                **(
                    {"repeat_index": repeat_index}
                    if repeat_index is not None
                    else {}
                ),
                "metric_count": len(metrics),
                "artifact_count": len(artifacts),
                "campaign_manifest": str(campaign_config.campaign_manifest_out),
                "campaign_evaluation": str(
                    campaign_config.campaign_evaluation_out
                ),
            },
        )

    def _run_paired_repeated_validation(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_manifest: dict[str, Any],
        raw_actions: tuple[dict[str, Any], ...],
        regression_dir: Path,
        repeat0_seed: int,
        repeat0_baseline: MatchedBaselineRun,
        repeat0_candidate: CandidateRepeatRun,
    ) -> dict[str, Any]:
        evidence_dir = regression_dir / "paired_validation"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        baseline_samples: list[dict[str, float | int]] = [repeat0_baseline.metrics]
        candidate_samples: list[dict[str, float | int]] = [repeat0_candidate.metrics]
        runs = [
            paired_validation_run_payload(
                repeat_index=0,
                seed=repeat0_seed,
                baseline_metrics=repeat0_baseline.metrics,
                candidate_metrics=repeat0_candidate.metrics,
                baseline_artifacts=repeat0_baseline.artifacts,
                candidate_artifacts=repeat0_candidate.artifacts,
            )
        ]
        run_error_count = 0
        for repeat_index in range(1, self.settings.paired_repeats):
            seed = repeat0_seed + repeat_index * self.settings.repeat_seed_stride
            repeat_dir = evidence_dir / f"repeat_{repeat_index:03d}"
            try:
                baseline = self._run_matched_noop_baseline(
                    task=task,
                    proposal=proposal,
                    patch=patch,
                    candidate_manifest=candidate_manifest,
                    baseline_manifest=baseline_manifest,
                    regression_dir=regression_dir,
                    matched_dir=repeat_dir / "matched_noop_baseline",
                    seed=seed,
                    run_role="matched_noop_baseline_repeat",
                    repeat_index=repeat_index,
                )
                candidate = self._run_combined_candidate_repeat(
                    task=task,
                    proposal=proposal,
                    patch=patch,
                    candidate_manifest=candidate_manifest,
                    baseline_manifest=baseline_manifest,
                    raw_actions=raw_actions,
                    repeat_dir=repeat_dir / "candidate",
                    seed=seed,
                    repeat_index=repeat_index,
                )
            except Exception as exc:  # noqa: BLE001 - repeat errors become evidence
                run_error_count += 1
                runs.append(
                    {
                        "repeat_index": repeat_index,
                        "seed": seed,
                        "status": "error",
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
                continue
            baseline_samples.append(baseline.metrics)
            candidate_samples.append(candidate.metrics)
            runs.append(
                paired_validation_run_payload(
                    repeat_index=repeat_index,
                    seed=seed,
                    baseline_metrics=baseline.metrics,
                    candidate_metrics=candidate.metrics,
                    baseline_artifacts=baseline.artifacts,
                    candidate_artifacts=candidate.artifacts,
                )
            )
        baseline_metrics = aggregate_metric_snapshots(baseline_samples)
        candidate_metrics = aggregate_metric_snapshots(candidate_samples)
        metric_stability = paired_metric_stability(
            baseline_samples,
            candidate_samples,
        )
        summary = {
            "enabled": True,
            "status": "error" if run_error_count else "passed",
            "requested_repeats": self.settings.paired_repeats,
            "complete_pair_count": len(baseline_samples),
            "run_error_count": run_error_count,
            "seed_strategy": {
                "base_seed": repeat0_seed,
                "repeat_seed_stride": self.settings.repeat_seed_stride,
            },
            "aggregate": "mean",
            **metric_stability["summary"],
        }
        payload = {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.harness_candidate_paired_validation",
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "summary": summary,
            "metric_stability": metric_stability["metrics"],
            "runs": runs,
        }
        artifact_path = evidence_dir / "candidate_paired_validation.json"
        _write_json(artifact_path, payload)
        return {
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "summary": summary,
            "artifact_path": str(artifact_path),
        }

    def _run_combined_candidate_repeat(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_manifest: dict[str, Any],
        raw_actions: tuple[dict[str, Any], ...],
        repeat_dir: Path,
        seed: int,
        repeat_index: int,
    ) -> CandidateRepeatRun:
        repeat_dir.mkdir(parents=True, exist_ok=True)
        repeat_candidate_id = ":".join(
            [
                str(candidate_manifest.get("candidate_id") or "candidate"),
                f"repeat_{repeat_index:03d}",
            ]
        )
        adapter_context = CandidateActionAdapterContext(
            candidate_id=repeat_candidate_id,
            regression_dir=repeat_dir,
        )
        adapter_results = adapt_candidate_actions(
            raw_actions,
            adapter_context,
            adapters=self.action_adapters,
            plugin_registry=self._plugin_registry(),
        )
        overlay = build_candidate_action_overlay(
            {**candidate_manifest, "candidate_id": repeat_candidate_id},
            adapter_results,
            kind=CANDIDATE_ACTION_OVERLAY_KIND,
        )
        overlay["selected_variant_id"] = "combined"
        overlay["repeat_index"] = repeat_index
        overlay_path = repeat_dir / "candidate_action_overlay.json"
        _write_json(overlay_path, overlay)
        directives = build_candidate_directives(adapter_results)
        directives_path = (
            repeat_dir / "candidate_mutation_directives.json"
            if directives["directives"]
            else None
        )
        if directives_path is not None:
            _write_json(directives_path, directives)
        runtime_metrics_path = repeat_dir / "candidate_runtime_metrics.json"
        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=repeat_dir,
            action_overlay=overlay_path,
            adapter_results=adapter_results,
            initial_directives=directives_path,
            runtime_metrics=runtime_metrics_path,
            seed=seed,
        )
        run_config_path = repeat_dir / "candidate_regression_config.json"
        config_payload = candidate_regression_config_payload(
            campaign_config,
            action_overlay=overlay_path,
            adapter_results=adapter_results,
            initial_directives=directives_path,
            runtime_metrics=runtime_metrics_path,
            run_role="candidate_repeat",
            settings=self.settings,
            kind=CANDIDATE_REGRESSION_CONFIG_KIND,
        )
        config_payload["repeat_index"] = repeat_index
        _write_json(run_config_path, config_payload)
        campaign_manifest = self._run_candidate_campaign(
            campaign_config,
            task=task,
            proposal=proposal,
            patch=patch,
        )
        campaign_evaluation = read_json_object(campaign_config.campaign_evaluation_out)
        metrics = candidate_metric_snapshot(
            campaign_manifest,
            campaign_evaluation,
            cwd=_campaign_cwd(campaign_manifest, campaign_config.cwd),
        )
        metrics.update(adapter_metric_snapshot(adapter_results))
        metrics.update(runtime_metric_snapshot(runtime_metrics_path))
        artifacts = {
            "candidate_regression_config": str(run_config_path),
            "candidate_action_overlay": str(overlay_path),
            **adapter_artifacts(adapter_results),
            **_optional_artifact("candidate_mutation_directives", directives_path),
            **_optional_existing_artifact(
                "candidate_runtime_metrics",
                runtime_metrics_path,
            ),
            "candidate_campaign_manifest": str(campaign_config.campaign_manifest_out),
            "candidate_campaign_evaluation": str(
                campaign_config.campaign_evaluation_out
            ),
        }
        return CandidateRepeatRun(
            metrics=metrics,
            artifacts=artifacts,
            evaluation=campaign_evaluation,
        )

    def _candidate_campaign_config(
        self,
        *,
        task: dict[str, Any],
        baseline_manifest: dict[str, Any],
        regression_dir: Path,
        action_overlay: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        initial_directives: Path | None,
        runtime_metrics: Path,
        seed: int | None = None,
    ) -> CampaignConfig:
        baseline_config = mapping(baseline_manifest.get("config"))
        baseline_artifacts = mapping(baseline_manifest.get("artifacts"))
        cwd = _path_or_none(baseline_manifest.get("cwd"))
        modes = self.settings.modes or tuple(
            str(mode)
            for mode in list_value(baseline_manifest.get("mode_names"))
            if mode is not None
        )
        if not modes:
            modes = ("heuristic_feedback",)
        extra_make_vars = tuple(
            str(item)
            for item in list_value(baseline_config.get("extra_make_vars"))
            if item is not None
        )
        extra_make_vars = (
            *extra_make_vars,
            f"HARNESS_OPTIMIZATION_ACTION_OVERLAY={action_overlay}",
            f"{RUNTIME_METRICS_OUT_ENV}={runtime_metrics}",
            *adapter_make_vars(adapter_results),
        )
        return CampaignConfig(
            target=str(task.get("target") or baseline_manifest.get("target") or ""),
            target_config=_artifact_path(
                baseline_artifacts,
                "target_manifest",
                cwd,
            ),
            initial_directives=initial_directives,
            out_dir=regression_dir / "campaign",
            libafl_manifest=(
                _artifact_path(baseline_artifacts, "libafl_manifest", cwd)
                or _artifact_path(
                    mapping(task.get("artifacts")),
                    "libafl_manifest",
                    cwd,
                )
                or Path("Cargo.toml")
            ),
            modes=modes,
            rounds=self.settings.rounds,
            iters=_setting_int(self.settings.iters, baseline_config, "iters", 256),
            max_seeds=_setting_int(
                self.settings.max_seeds,
                baseline_config,
                "max_seeds",
                32,
            ),
            seed=_setting_int(
                seed if seed is not None else self.settings.seed,
                baseline_config,
                "seed",
                1,
            ),
            cargo=str(baseline_config.get("cargo") or "cargo"),
            make=str(baseline_config.get("make") or "make"),
            verilog_sources=_optional_str(baseline_config.get("verilog_sources")),
            toplevel=_optional_str(baseline_config.get("toplevel")),
            verilator_coverage=str(
                baseline_config.get("verilator_coverage") or "verilator_coverage"
            ),
            extra_make_vars=extra_make_vars,
            cwd=cwd,
            campaign_manifest_out=regression_dir / "candidate_campaign_manifest.json",
            run_plan_profile=self.settings.run_plan_profile
            or _optional_str(baseline_config.get("run_plan_profile")),
            campaign_plan_profile=self.settings.campaign_plan_profile,
            round_evaluation=self.settings.round_evaluation,
            campaign_evaluation_out=regression_dir
            / "candidate_campaign_evaluation.json",
            ignore_functional_coverage=bool(
                baseline_config.get("ignore_functional_coverage", False)
            ),
            llm_model=_optional_str(baseline_config.get("llm_model")),
            require_real_llm=False,
        )

    def _run_candidate_campaign(
        self,
        config: CampaignConfig,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.observation_context or ObservationContext(
            run_id=":".join(
                [
                    str(task.get("run_id") or "run"),
                    "candidate",
                    safe_slug(str(proposal.get("proposal_id") or "proposal")),
                ]
            ),
            parent_event_id=_optional_str(patch.get("candidate_id")),
        )
        campaign = self.campaign_orchestrator_factory(
            config,
            context,
            topology=self.topology,
            run_backends=self.run_backends,
            run_plan_profiles=self.run_plan_profiles,
            campaign_plan_profiles=self.campaign_plan_profiles,
            evaluation_backends=self.evaluation_backends or EvaluationBackends(),
            run_orchestrator_factory=self.run_orchestrator_factory,
        )
        return campaign.run()

    def _run_additional_variant_evaluations(
        self,
        *,
        selected_variants: tuple[dict[str, Any], ...],
        raw_actions: tuple[dict[str, Any], ...],
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_manifest: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        regression_dir: Path,
    ) -> tuple[dict[str, Any], ...]:
        evaluations: list[dict[str, Any]] = []
        for variant in selected_variants:
            variant_id = str(variant.get("variant_id") or "")
            if variant_id == "combined":
                continue
            actions = filter_candidate_actions_for_variant(raw_actions, variant)
            if not actions:
                continue
            evaluations.append(
                self._run_single_variant_evaluation(
                    variant=variant,
                    actions=actions,
                    task=task,
                    proposal=proposal,
                    patch=patch,
                    candidate_manifest=candidate_manifest,
                    baseline_manifest=baseline_manifest,
                    baseline_metrics=baseline_metrics,
                    regression_dir=regression_dir,
                )
            )
        return tuple(evaluations)

    def _run_single_variant_evaluation(
        self,
        *,
        variant: dict[str, Any],
        actions: tuple[dict[str, Any], ...],
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_manifest: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        regression_dir: Path,
    ) -> dict[str, Any]:
        variant_id = str(variant.get("variant_id") or "variant")
        variant_dir = regression_dir / "variants" / safe_slug(variant_id)
        variant_dir.mkdir(parents=True, exist_ok=True)
        variant_candidate_id = ":".join(
            [
                str(candidate_manifest.get("candidate_id") or "candidate"),
                safe_slug(variant_id),
            ]
        )
        adapter_context = CandidateActionAdapterContext(
            candidate_id=variant_candidate_id,
            regression_dir=variant_dir,
        )
        adapter_results = adapt_candidate_actions(
            actions,
            adapter_context,
            adapters=self.action_adapters,
            plugin_registry=self._plugin_registry(),
        )
        overlay = build_candidate_action_overlay(
            {**candidate_manifest, "candidate_id": variant_candidate_id},
            adapter_results,
            kind=CANDIDATE_ACTION_OVERLAY_KIND,
        )
        overlay["selected_variant_id"] = variant_id
        overlay_path = variant_dir / "candidate_action_overlay.json"
        _write_json(overlay_path, overlay)
        directives = build_candidate_directives(adapter_results)
        directives_path = (
            variant_dir / "candidate_mutation_directives.json"
            if directives["directives"]
            else None
        )
        if directives_path is not None:
            _write_json(directives_path, directives)
        runtime_metrics_path = variant_dir / "candidate_runtime_metrics.json"
        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=variant_dir,
            action_overlay=overlay_path,
            adapter_results=adapter_results,
            initial_directives=directives_path,
            runtime_metrics=runtime_metrics_path,
        )
        run_config_path = variant_dir / "candidate_regression_config.json"
        _write_json(
            run_config_path,
            candidate_regression_config_payload(
                campaign_config,
                action_overlay=overlay_path,
                adapter_results=adapter_results,
                initial_directives=directives_path,
                runtime_metrics=runtime_metrics_path,
                settings=self.settings,
                kind=CANDIDATE_REGRESSION_CONFIG_KIND,
            ),
        )
        artifacts = {
            "candidate_regression_config": str(run_config_path),
            "candidate_action_overlay": str(overlay_path),
            **adapter_artifacts(adapter_results),
            **_optional_artifact("candidate_mutation_directives", directives_path),
        }
        try:
            campaign_manifest = self._run_candidate_campaign(
                campaign_config,
                task=task,
                proposal=proposal,
                patch=patch,
            )
            campaign_evaluation = read_json_object(
                campaign_config.campaign_evaluation_out
            )
            metrics = candidate_metric_snapshot(
                campaign_manifest,
                campaign_evaluation,
                cwd=_campaign_cwd(campaign_manifest, campaign_config.cwd),
            )
            metrics.update(adapter_metric_snapshot(adapter_results))
            metrics.update(runtime_metric_snapshot(runtime_metrics_path))
            status = "passed"
            error = None
            artifacts.update(
                {
                    **_optional_existing_artifact(
                        "candidate_runtime_metrics",
                        runtime_metrics_path,
                    ),
                    "candidate_campaign_manifest": str(
                        campaign_config.campaign_manifest_out
                    ),
                    "candidate_campaign_evaluation": str(
                        campaign_config.campaign_evaluation_out
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - variant failure is ranked down
            metrics = {}
            status = "error"
            error = {"type": type(exc).__name__, "message": str(exc)}
            artifacts.update(
                _optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                )
            )
        return candidate_variant_evaluation_payload(
            variant=variant,
            status=status,
            metrics=metrics,
            baseline_metrics=baseline_metrics,
            artifacts=artifacts,
            adapter_results=adapter_results,
            error=error,
        )

    def _not_run_report(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        run_config_path: Path,
        overlay_path: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        directives_path: Path | None,
        runtime_metrics_path: Path,
        plugin_provenance_path: Path,
        reason: str,
    ) -> dict[str, Any]:
        plugin_provenance = read_json_object(plugin_provenance_path)
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "source": type(self).__name__,
            "status": "not_run",
            "application_status": patch.get("status"),
            "baseline_source": "task_snapshot",
            "baseline_metrics": baseline_metrics,
            "source_baseline_metrics": baseline_metrics,
            "matched_baseline_metrics": None,
            "candidate_metrics": dict(baseline_metrics),
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "plugin_validation": mapping(plugin_provenance.get("validation")),
            "plugin_provenance": plugin_provenance,
            "reason": reason,
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                "candidate_plugin_provenance": str(plugin_provenance_path),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
            },
            "summary": {
                "matched_baseline": {
                    "enabled": self.settings.matched_baseline,
                    "status": "skipped",
                    "reason": reason,
                    "baseline_source": "task_snapshot",
                },
                **adapter_metric_snapshot(adapter_results),
                "candidate_variant_count": len(
                    build_candidate_variants(adapter_results)
                ),
            },
        }

    def _error_report(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        baseline_source: str = "task_snapshot",
        source_baseline_metrics: dict[str, float | int] | None = None,
        run_config_path: Path,
        overlay_path: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        directives_path: Path | None,
        runtime_metrics_path: Path,
        plugin_provenance_path: Path,
        error: BaseException,
        error_context: str = "candidate_campaign",
        matched_baseline_artifacts: dict[str, str] | None = None,
        matched_baseline_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plugin_provenance = read_json_object(plugin_provenance_path)
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "source": type(self).__name__,
            "status": "error",
            "baseline_source": baseline_source,
            "baseline_metrics": baseline_metrics,
            "source_baseline_metrics": source_baseline_metrics or baseline_metrics,
            "matched_baseline_metrics": (
                baseline_metrics if baseline_source == "matched_noop_rerun" else None
            ),
            "candidate_metrics": {},
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "plugin_validation": mapping(plugin_provenance.get("validation")),
            "plugin_provenance": plugin_provenance,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "context": error_context,
            },
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                "candidate_plugin_provenance": str(plugin_provenance_path),
                **(matched_baseline_artifacts or {}),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
            },
            "summary": {
                "matched_baseline": matched_baseline_summary
                or {
                    "enabled": self.settings.matched_baseline,
                    "status": "disabled",
                    "baseline_source": "task_snapshot",
                },
                **adapter_metric_snapshot(adapter_results),
                "candidate_variant_count": len(
                    build_candidate_variants(adapter_results)
                ),
            },
        }


def default_candidate_action_adapters(
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, CandidateActionAdapter]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    return {
        action_type: JsonConfigActionAdapter(
            action_type,
            plugin_registry=registry,
        )
        for action_type in registry.allowed_action_types()
        if registry.adapter_config_for(action_type) is not None
    }


def adapter_config_for(
    action_type: str,
    *,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> tuple[str, str, str]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    plugin_config = registry.adapter_config_for(action_type)
    if plugin_config is not None:
        return plugin_config
    slug = safe_slug(action_type)
    make_slug = slug.upper().replace("-", "_").replace(".", "_")
    return (
        f"libafl_bfm_fuzz.harness_candidate_{slug}_config",
        f"candidate_{slug}_config",
        f"HARNESS_{make_slug}_CONFIG",
    )


def load_candidate_actions(
    candidate_manifest: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    actions = []
    for item in list_value(candidate_manifest.get("candidate_artifacts")):
        if not isinstance(item, dict):
            continue
        artifact_path = _path_or_none(item.get("artifact_path"))
        action_payload = read_json_object(artifact_path) if artifact_path else {}
        actions.append(
            {
                "action_id": item.get("action_id"),
                "action_type": item.get("action_type"),
                "artifact_path": str(artifact_path) if artifact_path else None,
                "evidence_refs": list_value(item.get("evidence_refs")),
                "action": mapping(action_payload.get("action")),
            }
        )
    return tuple(actions)


def adapt_candidate_actions(
    actions: tuple[dict[str, Any], ...],
    context: CandidateActionAdapterContext,
    *,
    adapters: Mapping[str, CandidateActionAdapter] | None,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> tuple[CandidateActionAdapterResult, ...]:
    adapter_map = default_candidate_action_adapters(plugin_registry)
    if adapters is not None:
        adapter_map.update(adapters)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        action_type = _optional_str(action.get("action_type"))
        if action_type is None:
            continue
        by_type.setdefault(action_type, []).append(action)
    results = []
    for action_type, grouped_actions in sorted(by_type.items()):
        adapter = adapter_map.get(action_type)
        if adapter is None:
            adapter = JsonConfigActionAdapter(action_type)
        results.append(adapter.adapt(tuple(grouped_actions), context))
    return tuple(results)


def build_candidate_variant_ranking(
    *,
    action_overlay: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    selected_variant_id: str,
    variant_evaluations: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return _build_candidate_variant_ranking(
        action_overlay=action_overlay,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        selected_variant_id=selected_variant_id,
        variant_evaluations=variant_evaluations,
        kind=CANDIDATE_VARIANT_RANKING_KIND,
    )


def build_candidate_promotion_package(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    patch: dict[str, Any],
    candidate_manifest: dict[str, Any],
    candidate_metrics: dict[str, float | int],
    baseline_metrics: dict[str, float | int],
    ranking: dict[str, Any],
    thresholds: CandidateAcceptanceThresholds,
    stability_summary: dict[str, Any] | None = None,
    action_effect_report: dict[str, Any] | None = None,
    gap_actionability_report: dict[str, Any] | None = None,
    gap_actionability_minimal_proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _build_candidate_promotion_package(
        task=task,
        proposal=proposal,
        patch=patch,
        candidate_manifest=candidate_manifest,
        candidate_metrics=candidate_metrics,
        baseline_metrics=baseline_metrics,
        ranking=ranking,
        thresholds=thresholds,
        stability_summary=stability_summary,
        action_effect_report=action_effect_report,
        gap_actionability_report=gap_actionability_report,
        gap_actionability_minimal_proposal=gap_actionability_minimal_proposal,
        kind=CANDIDATE_PROMOTION_PACKAGE_KIND,
        minimal_candidate_kind=MINIMAL_PROMOTION_CANDIDATE_KIND,
    )


def build_action_pruning_summary(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    promotion_status: str,
    action_effect_report: dict[str, Any],
) -> dict[str, Any]:
    return _build_action_pruning_summary(
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        promotion_status=promotion_status,
        action_effect_report=action_effect_report,
        minimal_candidate_kind=MINIMAL_PROMOTION_CANDIDATE_KIND,
    )


def build_candidate_gap_actionability_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    baseline_campaign_manifest: dict[str, Any],
    candidate_campaign_manifest: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    action_effect_report: dict[str, Any],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    baseline_summary_path = latest_coverage_summary_path(baseline_campaign_manifest)
    candidate_summary_path = latest_coverage_summary_path(candidate_campaign_manifest)
    candidate_summary = (
        read_json_object(candidate_summary_path) if candidate_summary_path else {}
    )
    gap_context = HarnessGapActionabilityContext(
        target=str(task.get("target") or ""),
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
    )
    gaps = [
        classify_gap_actionability(
            gap,
            target=str(task.get("target") or ""),
            plugin_registry=registry,
            context=gap_context,
        )
        for gap in list_value(
            mapping(candidate_summary.get("rtl_gap_summary")).get("top_gaps")
        )
        if isinstance(gap, dict)
    ]
    action_summary = mapping(action_effect_report.get("summary"))
    categories: dict[str, int] = {}
    recommended_action_types: dict[str, int] = {}
    for gap in gaps:
        category = str(gap.get("actionability") or "unknown")
        categories[category] = categories.get(category, 0) + 1
        action_type = gap.get("recommended_action_type")
        if action_type:
            action_type_text = str(action_type)
            recommended_action_types[action_type_text] = (
                recommended_action_types.get(action_type_text, 0) + 1
            )
    baseline_uncovered = number_value(baseline_metrics.get("uncovered_line_count"))
    candidate_uncovered = number_value(candidate_metrics.get("uncovered_line_count"))
    delta = (
        candidate_uncovered - baseline_uncovered
        if baseline_uncovered is not None and candidate_uncovered is not None
        else None
    )
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_gap_actionability_report",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "baseline_coverage_summary": str(baseline_summary_path)
        if baseline_summary_path
        else None,
        "candidate_coverage_summary": str(candidate_summary_path)
        if candidate_summary_path
        else None,
        "baseline_uncovered_line_count": baseline_uncovered,
        "candidate_uncovered_line_count": candidate_uncovered,
        "uncovered_line_delta": delta,
        "remaining_gaps": gaps,
        "plugin_registry": plugin_registry_to_json(registry),
        "plugin_validation": plugin_registry_validation_json(registry),
        "plugin_provenance": plugin_registry_provenance_json(registry),
        "summary": {
            "baseline_uncovered_line_count": baseline_uncovered,
            "candidate_uncovered_line_count": candidate_uncovered,
            "uncovered_line_delta": delta,
            "remaining_gap_count": len(gaps),
            "actionability_counts": categories,
            "recommended_action_type_counts": recommended_action_types,
            "effective_action_count": int(
                number_value(action_summary.get("improved_action_count")) or 0
            ),
            "neutral_action_count": int(
                number_value(action_summary.get("neutral_action_count")) or 0
            ),
            "harmful_action_count": int(
                number_value(action_summary.get("regressed_action_count")) or 0
            ),
            "blocked_by_action_surface": any(
                gap.get("actionability")
                in {"requires_mmio_write_surface", "requires_internal_state_surface"}
                for gap in gaps
            ),
            "has_mmio_readback_targets": any(
                gap.get("recommended_action_type") == "mmio_readback"
                for gap in gaps
            ),
        },
    }


def build_gap_actionability_minimal_candidate_proposal(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    gap_actionability_report: dict[str, Any],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    safe_action_types = set(registry.safe_sandbox_action_types())
    allowed_action_types = set(registry.allowed_action_types())
    payload_required_types = set(registry.dsl_payload_action_types())
    proposal_refs = list_value(proposal.get("evidence_refs"))
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    action_by_key: dict[str, dict[str, Any]] = {}
    for gap in list_value(gap_actionability_report.get("remaining_gaps")):
        if not isinstance(gap, dict):
            continue
        action_type = str(gap.get("recommended_action_type") or "")
        gap_id = str(gap.get("id") or f"gap_{len(actions) + len(skipped)}")
        payload = mapping(gap.get("suggested_payload"))
        evidence_refs = list_value(gap.get("evidence_refs")) or proposal_refs
        if not action_type:
            skipped.append(_skipped_gap_recommendation(gap, reason="no_action_type"))
            continue
        if action_type not in allowed_action_types:
            skipped.append(
                _skipped_gap_recommendation(
                    gap,
                    reason="action_type_not_registered",
                    action_type=action_type,
                )
            )
            continue
        if action_type not in safe_action_types:
            skipped.append(
                _skipped_gap_recommendation(
                    gap,
                    reason="action_type_not_safe_for_sandbox",
                    action_type=action_type,
                )
            )
            continue
        if action_type in payload_required_types and not payload:
            skipped.append(
                _skipped_gap_recommendation(
                    gap,
                    reason="missing_suggested_payload",
                    action_type=action_type,
                )
            )
            continue
        if not evidence_refs:
            skipped.append(
                _skipped_gap_recommendation(
                    gap,
                    reason="missing_evidence_refs",
                    action_type=action_type,
                )
            )
            continue
        payload_errors = registry.action_payload_errors(
            action_type,
            payload,
            path="suggested_payload",
        )
        if payload_errors:
            skipped.append(
                {
                    **_skipped_gap_recommendation(
                        gap,
                        reason="invalid_suggested_payload",
                        action_type=action_type,
                    ),
                    "payload_errors": payload_errors,
                }
            )
            continue
        key = _action_payload_key(action_type, payload)
        if key in action_by_key:
            action = action_by_key[key]
            action.setdefault("source_gap_ids", []).append(gap_id)
            continue
        action = {
            "action_id": (
                f"gap_{len(actions):02d}_{safe_slug(action_type)}_{safe_slug(gap_id)}"
            ),
            "action_type": action_type,
            "payload": payload,
            "rationale": gap.get("actionability_reason"),
            "evidence_refs": evidence_refs,
            "source_gap_ids": [gap_id],
        }
        actions.append(action)
        action_by_key[key] = action
    status = "proposed" if actions else "no_op"
    minimal = {
        "schema_version": 1,
        "kind": PROPOSAL_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": (
            f"{candidate_manifest.get('candidate_id') or 'candidate'}:"
            "gap_actionability_minimal"
        ),
        "status": status,
        "source": "candidate_gap_actionability_report",
        "source_candidate_id": candidate_manifest.get("candidate_id"),
        "actions": actions,
        "evidence_refs": proposal_refs,
        "skipped_recommendations": skipped,
        "plugin_registry": plugin_registry_to_json(registry),
        "plugin_validation": plugin_registry_validation_json(registry),
        "plugin_provenance": plugin_registry_provenance_json(registry),
        "summary": {
            "remaining_gap_count": len(
                list_value(gap_actionability_report.get("remaining_gaps"))
            ),
            "recommended_action_count": len(actions),
            "skipped_recommendation_count": len(skipped),
            "status": status,
        },
    }
    minimal["validation"] = validate_harness_optimization_proposal(
        minimal,
        task=task,
        plugin_registry=registry,
    )
    return minimal


def _skipped_gap_recommendation(
    gap: Mapping[str, Any],
    *,
    reason: str,
    action_type: str | None = None,
) -> dict[str, Any]:
    return {
        "gap_id": gap.get("id"),
        "actionability": gap.get("actionability"),
        "recommended_action_type": action_type or gap.get("recommended_action_type"),
        "reason": reason,
    }


def _action_payload_key(action_type: str, payload: Mapping[str, Any]) -> str:
    return json.dumps(
        {"action_type": action_type, "payload": dict(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )


def latest_coverage_summary_path(campaign_manifest: dict[str, Any]) -> Path | None:
    result: Path | None = None
    cwd = _campaign_cwd(campaign_manifest, None)
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            path = _artifact_path(
                mapping(round_item.get("artifacts")),
                "coverage_summary",
                cwd,
            )
            if path is not None:
                result = path
    return result


def classify_gap_actionability(
    gap: dict[str, Any],
    *,
    target: str,
    plugin_registry: HarnessPluginRegistry | None = None,
    context: HarnessGapActionabilityContext | None = None,
) -> dict[str, Any]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    gap_context = context or HarnessGapActionabilityContext(target=target)
    return registry.classify_gap_actionability(gap, gap_context)


def default_candidate_regression_plugin_registry() -> HarnessPluginRegistry:
    return default_harness_plugin_registry()


def candidate_regression_plugin_registry(
    plugin_registry: HarnessPluginRegistry | None = None,
) -> HarnessPluginRegistry:
    base = default_candidate_regression_plugin_registry()
    return base if plugin_registry is None else base.merge(plugin_registry)


def build_candidate_action_effect_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    variant_evaluations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    variants = []
    consumed_action_ids: set[str] = set()
    runtime_action_ids: set[str] = set()
    for evaluation in variant_evaluations:
        runtime_payload = read_runtime_metrics(
            _path_or_none(
                mapping(evaluation.get("artifacts")).get("candidate_runtime_metrics")
            )
        )
        actions = build_variant_action_effects(
            evaluation=evaluation,
            runtime_payload=runtime_payload,
            baseline_metrics=baseline_metrics,
            plugin_registry=registry,
        )
        for action in actions:
            if action.get("is_runtime_action"):
                runtime_action_ids.add(str(action.get("action_id")))
            if action.get("consumed"):
                consumed_action_ids.add(str(action.get("action_id")))
        variants.append(
            {
                "variant_id": evaluation.get("variant_id"),
                "variant_type": evaluation.get("variant_type"),
                "status": evaluation.get("status"),
                "action_ids": list_value(evaluation.get("action_ids")),
                "metric_delta_summary": metric_change_summary(
                    baseline_metrics,
                    numeric_variant_metrics(evaluation),
                ),
                "actions": actions,
            }
        )
    action_ids = {
        str(action.get("action_id"))
        for evaluation in variant_evaluations
        for action in list_value(evaluation.get("actions"))
        if isinstance(action, dict) and action.get("action_id") is not None
    }
    aggregate_actions = aggregate_action_effects(variants)
    effect_counts = action_effect_status_counts(aggregate_actions)
    standalone_actions = [
        action
        for action in aggregate_actions
        if action.get("standalone_effect_status") is not None
    ]
    standalone_effect_counts = action_effect_status_counts(
        standalone_actions,
        status_key="standalone_effect_status",
        prefix="standalone_",
    )
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_action_effect_report",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "plugin_registry": plugin_registry_to_json(registry),
        "plugin_validation": plugin_registry_validation_json(registry),
        "plugin_provenance": plugin_registry_provenance_json(registry),
        "variants": variants,
        "actions": aggregate_actions,
        "summary": {
            "variant_count": len(variants),
            "action_count": len(action_ids),
            "runtime_action_count": len(runtime_action_ids),
            "consumed_action_count": len(consumed_action_ids),
            "standalone_evaluated_action_count": len(standalone_actions),
            **effect_counts,
            **standalone_effect_counts,
        },
    }


def build_variant_action_effects(
    *,
    evaluation: dict[str, Any],
    runtime_payload: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> list[dict[str, Any]]:
    registry = candidate_regression_plugin_registry(plugin_registry)
    runtime_action_types = set(registry.runtime_action_types())
    runtime_by_action = runtime_action_metric_map(runtime_payload)
    section_metrics = runtime_section_metric_map(runtime_payload)
    actions = [
        action
        for action in list_value(evaluation.get("actions"))
        if isinstance(action, dict)
    ]
    type_counts: dict[str, int] = {}
    for action in actions:
        action_type = str(action.get("action_type") or "")
        type_counts[action_type] = type_counts.get(action_type, 0) + 1
    results = []
    for action in actions:
        action_id = str(action.get("action_id") or "")
        action_type = str(action.get("action_type") or "")
        metrics = dict(runtime_by_action.get((action_id, action_type), {}))
        if not metrics and type_counts.get(action_type) == 1:
            metrics = dict(section_metrics.get(action_type, {}))
        is_runtime_action = action_type in runtime_action_types
        consumed = bool(metrics) if is_runtime_action else False
        delta_summary = metric_change_summary(
            baseline_metrics,
            numeric_variant_metrics(evaluation),
        )
        results.append(
            {
                "action_id": action_id,
                "action_type": action_type,
                "is_runtime_action": is_runtime_action,
                "consumed": consumed,
                "consumption_status": (
                    "consumed"
                    if consumed
                    else "not_consumed"
                    if is_runtime_action
                    else "not_runtime_action"
                ),
                "effect_status": classify_action_effect(
                    is_runtime_action=is_runtime_action,
                    consumed=consumed,
                    delta_summary=delta_summary,
                ),
                "runtime_metrics": metrics,
                "variant_metric_delta_summary": delta_summary,
                "evidence_refs": list_value(action.get("evidence_refs")),
            }
        )
    return results


def classify_action_effect(
    *,
    is_runtime_action: bool,
    consumed: bool,
    delta_summary: dict[str, Any],
) -> str:
    if is_runtime_action and not consumed:
        return "not_consumed"
    if (
        int(number_value(delta_summary.get("gateable_regressed_metric_count")) or 0)
        > 0
    ):
        return "regressed"
    if (
        int(number_value(delta_summary.get("gateable_improved_metric_count")) or 0)
        > 0
    ):
        return "improved"
    return "neutral"


def aggregate_action_effects(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_action: dict[str, dict[str, Any]] = {}
    for variant in variants:
        variant_id = str(variant.get("variant_id") or "")
        variant_type = str(variant.get("variant_type") or "")
        variant_action_ids = [
            str(action_id)
            for action_id in list_value(variant.get("action_ids"))
            if action_id is not None
        ]
        for action in list_value(variant.get("actions")):
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("action_id") or "")
            if not action_id:
                continue
            action_status = str(action.get("effect_status") or "neutral")
            entry = by_action.setdefault(
                action_id,
                {
                    "action_id": action_id,
                    "action_type": action.get("action_type"),
                    "is_runtime_action": action.get("is_runtime_action"),
                    "consumed": False,
                    "standalone_effect_status": None,
                    "combined_effect_status": None,
                    "aggregate_effect_status": "neutral",
                    "effect_status": "neutral",
                    "supporting_variants": [],
                    "evidence_refs": list_value(action.get("evidence_refs")),
                },
            )
            entry["consumed"] = bool(entry.get("consumed") or action.get("consumed"))
            entry["aggregate_effect_status"] = merge_action_effect_status(
                str(entry.get("aggregate_effect_status") or "neutral"),
                action_status,
            )
            if variant_type == "single_action" or variant_action_ids == [action_id]:
                entry["standalone_effect_status"] = merge_optional_action_effect_status(
                    entry.get("standalone_effect_status"),
                    action_status,
                )
            elif variant_type == "combined_actions" or len(variant_action_ids) > 1:
                entry["combined_effect_status"] = merge_optional_action_effect_status(
                    entry.get("combined_effect_status"),
                    action_status,
                )
            entry["supporting_variants"].append(
                {
                    "variant_id": variant_id,
                    "variant_type": variant.get("variant_type"),
                    "status": action.get("effect_status"),
                    "consumed": action.get("consumed"),
                    "metric_delta_summary": action.get(
                        "variant_metric_delta_summary"
                    ),
                }
            )
    for entry in by_action.values():
        standalone_status = entry.get("standalone_effect_status")
        entry["effect_status"] = str(
            standalone_status
            if standalone_status is not None
            else entry.get("aggregate_effect_status")
            or "neutral"
        )
    return sorted(by_action.values(), key=lambda item: str(item.get("action_id")))


def merge_optional_action_effect_status(current: Any, new: str) -> str:
    if current is None:
        return new
    return merge_action_effect_status(str(current), new)


def merge_action_effect_status(current: str, new: str) -> str:
    priority = {
        "regressed": 4,
        "improved": 3,
        "neutral": 2,
        "not_consumed": 1,
    }
    return new if priority.get(new, 0) > priority.get(current, 0) else current


def action_effect_status_counts(
    actions: list[dict[str, Any]],
    *,
    status_key: str = "effect_status",
    prefix: str = "",
) -> dict[str, int]:
    counts = {
        f"{prefix}improved_action_count": 0,
        f"{prefix}neutral_action_count": 0,
        f"{prefix}regressed_action_count": 0,
        f"{prefix}not_consumed_action_count": 0,
    }
    for action in actions:
        status = str(action.get(status_key) or "neutral")
        key = f"{prefix}{status}_action_count"
        if key in counts:
            counts[key] += 1
    return counts


def runtime_action_metric_map(
    payload: dict[str, Any],
) -> dict[tuple[str, str], dict[str, float | int]]:
    result: dict[tuple[str, str], dict[str, float | int]] = {}
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return result
    for section, metrics in sections.items():
        if not isinstance(metrics, dict):
            continue
        for action in list_value(metrics.get("actions")):
            if not isinstance(action, dict):
                continue
            action_id = _optional_str(action.get("action_id"))
            action_type = _optional_str(action.get("action_type")) or str(section)
            if action_id is None:
                continue
            values: dict[str, float | int] = {}
            _merge_numeric_metrics(values, action)
            result[(action_id, action_type)] = values
    return result


def runtime_section_metric_map(
    payload: dict[str, Any],
) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return result
    for section, metrics in sections.items():
        if not isinstance(metrics, dict):
            continue
        values: dict[str, float | int] = {}
        _merge_numeric_metrics(values, metrics)
        result[str(section)] = values
    return result


def candidate_metric_snapshot(
    campaign_manifest: dict[str, Any],
    campaign_evaluation: dict[str, Any],
    *,
    cwd: Path | None,
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for source in (
        mapping(campaign_evaluation.get("summary")),
        mapping(mapping(campaign_evaluation.get("harness_trace")).get("summary")),
    ):
        _merge_numeric_metrics(metrics, source)
    harness_evaluation = _harness_evaluation(campaign_evaluation, cwd)
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("summary")))
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("trace_quality")))
    _merge_campaign_round_metrics(metrics, campaign_manifest)
    _merge_campaign_runtime_metrics(metrics, campaign_manifest, cwd=cwd)
    return metrics


def _merge_campaign_round_metrics(
    metrics: dict[str, float | int],
    campaign_manifest: dict[str, Any],
) -> None:
    last_coverage: dict[str, Any] = {}
    directive_count = None
    case_count = 0
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            coverage = mapping(round_item.get("coverage"))
            if coverage:
                last_coverage = coverage
            feedback = mapping(round_item.get("feedback"))
            if feedback.get("directive_count") is not None:
                directive_count = feedback.get("directive_count")
            stages = mapping(round_item.get("stages"))
            corpus_validation = mapping(
                mapping(stages.get("corpus_validation")).get("validation")
            )
            case_count += int(number_value(corpus_validation.get("case_count")) or 0)
    _merge_numeric_metrics(metrics, last_coverage)
    if directive_count is not None:
        value = number_value(directive_count)
        if value is not None:
            metrics["directive_count"] = value
    if case_count:
        metrics["case_count"] = case_count


def _merge_campaign_runtime_metrics(
    metrics: dict[str, float | int],
    campaign_manifest: dict[str, Any],
    *,
    cwd: Path | None,
) -> None:
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            path = _artifact_path(
                mapping(round_item.get("artifacts")),
                "harness_runtime_metrics",
                cwd,
            )
            _merge_numeric_metrics(metrics, runtime_metric_snapshot(path))


def _harness_evaluation(
    campaign_evaluation: dict[str, Any],
    cwd: Path | None,
) -> dict[str, Any]:
    artifacts = mapping(
        mapping(campaign_evaluation.get("harness_trace")).get("artifacts")
    )
    path = _path_or_none(artifacts.get("harness_evaluation"))
    if path is None:
        return {}
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    return read_json_object(path)


def _merge_numeric_metrics(
    target: dict[str, float | int],
    values: dict[str, Any],
) -> None:
    for key, value in values.items():
        number = number_value(value)
        if number is not None:
            target[str(key)] = number


def _setting_int(
    value: int | None,
    baseline_config: dict[str, Any],
    key: str,
    default: int,
) -> int:
    if value is not None:
        return value
    baseline_value = number_value(baseline_config.get(key))
    return int(baseline_value if baseline_value is not None else default)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
