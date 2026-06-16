from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ConnectGraph import ObservationContext
from ConnectGraph.trace import read_json_object

from ..campaign_orchestrator import CampaignConfig, CampaignOrchestrator
from .optimization import (
    CANDIDATE_EVALUATION_KIND,
    PROPOSAL_KIND,
    baseline_metric_snapshot,
    default_harness_plugin_registry,
    list_value,
    metric_direction,
    metric_gates_acceptance,
    number_value,
    safe_slug,
    utc_timestamp,
    validate_harness_optimization_proposal,
)
from .plugins import (
    HarnessGapActionabilityContext,
    HarnessPluginRegistry,
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
from harness_optimization.runtime import (
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


ADAPTER_CONFIG_KINDS = {
    "mutation_directive_update": (
        "libafl_bfm_fuzz.harness_candidate_mutation_directive_updates",
        "candidate_mutation_directive_updates",
        "HARNESS_MUTATION_DIRECTIVE_UPDATE_CONFIG",
    ),
    "replay_probe": (
        "libafl_bfm_fuzz.harness_candidate_replay_probe_config",
        "candidate_replay_probe_config",
        "HARNESS_REPLAY_PROBE_CONFIG",
    ),
    "scoreboard_check": (
        "libafl_bfm_fuzz.harness_candidate_scoreboard_check_config",
        "candidate_scoreboard_check_config",
        "HARNESS_SCOREBOARD_CHECK_CONFIG",
    ),
    "coverage_feedback_tuning": (
        "libafl_bfm_fuzz.harness_candidate_coverage_feedback_tuning_config",
        "candidate_coverage_feedback_tuning_config",
        "HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG",
    ),
    "mmio_readback": (
        "libafl_bfm_fuzz.harness_candidate_mmio_readback_config",
        "candidate_mmio_readback_config",
        "HARNESS_MMIO_READBACK_CONFIG",
    ),
    "stimulus_generation_hint": (
        "libafl_bfm_fuzz.harness_candidate_stimulus_hint_config",
        "candidate_stimulus_hint_config",
        "HARNESS_STIMULUS_HINT_CONFIG",
    ),
    "documentation_note": (
        "libafl_bfm_fuzz.harness_candidate_documentation_notes",
        "candidate_documentation_notes",
        "HARNESS_DOCUMENTATION_NOTES",
    ),
}

RUNTIME_ACTION_TYPES = {
    "replay_probe",
    "scoreboard_check",
    "coverage_feedback_tuning",
    "mmio_readback",
}


@dataclass(frozen=True)
class CandidateAcceptanceThresholds:
    max_regressed_metric_count: int = 0
    min_improved_metric_count: int = 1
    max_flaky_metric_count: int = 0
    accepted_candidate_statuses: tuple[str, ...] = ("passed", "ok")

    def to_json(self) -> dict[str, Any]:
        return {
            "max_regressed_metric_count": self.max_regressed_metric_count,
            "min_improved_metric_count": self.min_improved_metric_count,
            "max_flaky_metric_count": self.max_flaky_metric_count,
            "accepted_candidate_statuses": list(self.accepted_candidate_statuses),
        }


@dataclass(frozen=True)
class CandidateRegressionSettings:
    modes: tuple[str, ...] | None = None
    rounds: int = 1
    iters: int | None = None
    max_seeds: int | None = None
    seed: int | None = None
    max_variant_regressions: int = 1
    run_plan_profile: str | None = None
    round_evaluation: bool = True
    campaign_plan_profile: str = "campaign_with_evaluation"
    matched_baseline: bool = False
    paired_repeats: int = 1
    repeat_seed_stride: int = 1
    attribution_top_k: int | None = None
    attribution_mode: str = "top_k"
    strict_plugin_validation: bool = False
    thresholds: CandidateAcceptanceThresholds = CandidateAcceptanceThresholds()

    def __post_init__(self) -> None:
        if self.max_variant_regressions < 1:
            raise ValueError("max_variant_regressions must be >= 1")
        if self.paired_repeats < 1:
            raise ValueError("paired_repeats must be >= 1")
        if self.repeat_seed_stride < 1:
            raise ValueError("repeat_seed_stride must be >= 1")
        if self.attribution_top_k is not None and self.attribution_top_k < 1:
            raise ValueError("attribution_top_k must be >= 1")
        if self.attribution_mode not in {"top_k", "all_actions"}:
            raise ValueError("attribution_mode must be one of: top_k, all_actions")

    def to_json(self) -> dict[str, Any]:
        return {
            "modes": list(self.modes) if self.modes is not None else None,
            "rounds": self.rounds,
            "iters": self.iters,
            "max_seeds": self.max_seeds,
            "seed": self.seed,
            "max_variant_regressions": self.max_variant_regressions,
            "run_plan_profile": self.run_plan_profile,
            "round_evaluation": self.round_evaluation,
            "campaign_plan_profile": self.campaign_plan_profile,
            "matched_baseline": self.matched_baseline,
            "paired_repeats": self.paired_repeats,
            "repeat_seed_stride": self.repeat_seed_stride,
            "attribution_top_k": self.attribution_top_k,
            "attribution_mode": self.attribution_mode,
            "strict_plugin_validation": self.strict_plugin_validation,
            "thresholds": self.thresholds.to_json(),
        }


@dataclass(frozen=True)
class CandidateActionAdapterContext:
    candidate_id: str
    regression_dir: Path


@dataclass(frozen=True)
class MatchedBaselineRun:
    metrics: dict[str, float | int]
    artifacts: dict[str, str]
    summary: dict[str, Any]


@dataclass(frozen=True)
class CandidateRepeatRun:
    metrics: dict[str, float | int]
    artifacts: dict[str, str]
    evaluation: dict[str, Any]


@dataclass(frozen=True)
class CandidateActionAdapterResult:
    action_type: str
    artifact_role: str
    artifact_path: Path | None
    make_var: str | None
    entries: tuple[dict[str, Any], ...]
    directives: tuple[dict[str, Any], ...] = ()
    variants: tuple[dict[str, Any], ...] = ()
    metric_counts: Mapping[str, int] | None = None

    def artifact_json(self) -> dict[str, str]:
        if self.artifact_path is None:
            return {}
        return {self.artifact_role: str(self.artifact_path)}

    def make_var_assignment(self) -> str | None:
        if self.make_var is None or self.artifact_path is None:
            return None
        return f"{self.make_var}={self.artifact_path}"

    def metric_json(self) -> dict[str, int]:
        return dict(self.metric_counts or {})


class CandidateActionAdapter(Protocol):
    action_type: str

    def adapt(
        self,
        actions: tuple[dict[str, Any], ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        ...


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
        plugin_validation = plugin_registry.validation_json()
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
        _write_json(plugin_provenance_path, plugin_registry.provenance_json())
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
        overlay = build_candidate_action_overlay(candidate_manifest, adapter_results)
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
            "plugin_provenance": plugin_registry.provenance_json(),
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
    registry = plugin_registry or default_candidate_regression_plugin_registry()
    plugin_config = registry.adapter_config_for(action_type)
    if plugin_config is not None:
        return plugin_config
    config = ADAPTER_CONFIG_KINDS.get(action_type)
    if config is not None:
        return config
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


def build_candidate_action_overlay(
    candidate_manifest: dict[str, Any],
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, Any]:
    variants = build_candidate_variants(adapter_results)
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_optimization_candidate_action_overlay",
        "created_at": utc_timestamp(),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "actions": [
            entry
            for result in adapter_results
            for entry in result.entries
        ],
        "adapter_results": [
            {
                "action_type": result.action_type,
                "artifact_role": result.artifact_role,
                "artifact_path": str(result.artifact_path)
                if result.artifact_path is not None
                else None,
                "make_var": result.make_var,
                "entry_count": len(result.entries),
                "variant_count": len(result.variants),
            }
            for result in adapter_results
        ],
        "variants": variants,
        "selected_variant_id": "combined" if variants else None,
    }


def build_candidate_directives(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, Any]:
    directives = [
        directive
        for result in adapter_results
        for directive in result.directives
    ]
    return {
        "schema_version": 1,
        "source": "harness_optimization_candidate",
        "directives": directives,
    }


def action_entry_for(action: dict[str, Any]) -> dict[str, Any]:
    payload = mapping(mapping(action.get("action")).get("payload"))
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "artifact_path": action.get("artifact_path"),
        "evidence_refs": list_value(action.get("evidence_refs")),
        "payload": payload,
        "rationale": mapping(action.get("action")).get("rationale"),
    }


def directives_from_action_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if entry.get("action_type") != "mutation_directive_update":
        return ()
    payload = mapping(entry.get("payload"))
    if isinstance(payload.get("directives"), list):
        return tuple(
            directive
            for directive in payload["directives"]
            if isinstance(directive, dict)
        )
    if isinstance(payload.get("directive"), dict):
        return (payload["directive"],)
    if payload:
        return (
            {
                "source": "harness_optimization_candidate",
                "action_id": entry.get("action_id"),
                "payload": payload,
            },
        )
    return ()


def action_variant(
    entry: dict[str, Any],
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    action_id = str(entry.get("action_id") or entry.get("action_type") or "action")
    return {
        "variant_id": f"action_{safe_slug(action_id)}",
        "variant_type": "single_action",
        "action_ids": [entry.get("action_id")],
        "action_types": [entry.get("action_type")],
        "artifact_paths": [str(artifact_path)],
        "validation_status": "materialized_not_run",
    }


def build_candidate_variants(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> list[dict[str, Any]]:
    single_action_variants = [
        variant
        for result in adapter_results
        for variant in result.variants
    ]
    if not single_action_variants:
        return []
    combined = {
        "variant_id": "combined",
        "variant_type": "combined_actions",
        "action_ids": [
            action_id
            for variant in single_action_variants
            for action_id in list_value(variant.get("action_ids"))
        ],
        "action_types": sorted(
            {
                str(action_type)
                for variant in single_action_variants
                for action_type in list_value(variant.get("action_types"))
                if action_type is not None
            }
        ),
        "artifact_paths": sorted(
            {
                str(path)
                for variant in single_action_variants
                for path in list_value(variant.get("artifact_paths"))
                if path is not None
            }
        ),
        "validation_status": "selected_for_regression",
    }
    return [combined, *single_action_variants]


def select_candidate_variants(
    variants: Any,
    *,
    max_count: int,
    attribution_mode: str = "top_k",
) -> tuple[dict[str, Any], ...]:
    if max_count < 1:
        raise ValueError("max_count must be >= 1")
    if attribution_mode not in {"top_k", "all_actions"}:
        raise ValueError("attribution_mode must be one of: top_k, all_actions")
    selected = [
        variant
        for variant in list_value(variants)
        if isinstance(variant, dict)
    ]
    if attribution_mode == "all_actions":
        return tuple(selected)
    return tuple(selected[:max_count])


def filter_candidate_actions_for_variant(
    actions: tuple[dict[str, Any], ...],
    variant: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    action_ids = {
        str(action_id)
        for action_id in list_value(variant.get("action_ids"))
        if action_id is not None
    }
    if not action_ids:
        return ()
    return tuple(
        action
        for action in actions
        if str(action.get("action_id")) in action_ids
    )


def adapter_make_vars(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> tuple[str, ...]:
    values = [
        value
        for result in adapter_results
        if (value := result.make_var_assignment()) is not None
    ]
    return tuple(values)


def adapter_artifacts(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for result in adapter_results:
        artifacts.update(result.artifact_json())
    return artifacts


def adapter_metric_snapshot(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, int]:
    metrics = {
        "candidate_action_count": sum(len(result.entries) for result in adapter_results),
        "candidate_overlay_count": len(adapter_results),
        "candidate_variant_count": len(build_candidate_variants(adapter_results)),
    }
    for result in adapter_results:
        metrics.update(result.metric_json())
    metrics["candidate_directive_count"] = sum(
        len(result.directives) for result in adapter_results
    )
    return metrics


def paired_validation_run_payload(
    *,
    repeat_index: int,
    seed: int,
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    baseline_artifacts: dict[str, str],
    candidate_artifacts: dict[str, str],
) -> dict[str, Any]:
    return {
        "repeat_index": repeat_index,
        "seed": seed,
        "status": "passed",
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "metric_delta_summary": metric_change_summary(
            baseline_metrics,
            candidate_metrics,
        ),
        "artifacts": {
            "baseline": baseline_artifacts,
            "candidate": candidate_artifacts,
        },
    }


def aggregate_metric_snapshots(
    samples: list[dict[str, float | int]] | tuple[dict[str, float | int], ...],
) -> dict[str, float | int]:
    metric_names = sorted(
        {
            metric
            for sample in samples
            for metric, value in sample.items()
            if number_value(value) is not None
        }
    )
    return {
        metric: aggregate_metric_value(
            [
                number
                for sample in samples
                if (number := number_value(sample.get(metric))) is not None
            ]
        )
        for metric in metric_names
    }


def aggregate_metric_value(values: list[float | int]) -> float | int:
    if not values:
        return 0
    mean = sum(values) / len(values)
    if all(isinstance(value, int) for value in values) and mean.is_integer():
        return int(mean)
    return mean


def paired_metric_stability(
    baseline_samples: list[dict[str, float | int]],
    candidate_samples: list[dict[str, float | int]],
) -> dict[str, Any]:
    metrics = []
    flaky_metric_count = 0
    gateable_metric_count = 0
    for metric in sorted(
        {
            key
            for sample in [*baseline_samples, *candidate_samples]
            for key, value in sample.items()
            if number_value(value) is not None
        }
    ):
        pair_deltas: list[float | int] = []
        directions: list[str] = []
        baseline_values = [
            number
            for sample in baseline_samples
            if (number := number_value(sample.get(metric))) is not None
        ]
        candidate_values = [
            number
            for sample in candidate_samples
            if (number := number_value(sample.get(metric))) is not None
        ]
        for baseline, candidate in zip(baseline_samples, candidate_samples):
            baseline_value = number_value(baseline.get(metric))
            candidate_value = number_value(candidate.get(metric))
            if baseline_value is None or candidate_value is None:
                continue
            delta = candidate_value - baseline_value
            pair_deltas.append(delta)
            directions.append(metric_direction(metric, delta))
        gates_acceptance = metric_gates_acceptance(metric)
        direction_set = sorted(set(directions))
        flaky = gates_acceptance and len(direction_set) > 1
        if gates_acceptance:
            gateable_metric_count += 1
        if flaky:
            flaky_metric_count += 1
        metrics.append(
            {
                "metric": metric,
                "role": "quality_gate" if gates_acceptance else "informational",
                "gates_acceptance": gates_acceptance,
                "sample_count": len(pair_deltas),
                "baseline_mean": aggregate_metric_value(baseline_values)
                if baseline_values
                else None,
                "candidate_mean": aggregate_metric_value(candidate_values)
                if candidate_values
                else None,
                "baseline_worst": worst_metric_value(metric, baseline_values),
                "candidate_worst": worst_metric_value(metric, candidate_values),
                "baseline_variance": metric_variance(baseline_values),
                "candidate_variance": metric_variance(candidate_values),
                "paired_delta_mean": aggregate_metric_value(pair_deltas)
                if pair_deltas
                else None,
                "paired_delta_worst": worst_paired_delta(metric, pair_deltas),
                "paired_directions": direction_set,
                "flaky": flaky,
            }
        )
    return {
        "summary": {
            "metric_count": len(metrics),
            "gateable_metric_count": gateable_metric_count,
            "flaky_metric_count": flaky_metric_count,
        },
        "metrics": metrics,
    }


def metric_variance(values: list[float | int]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def worst_metric_value(metric: str, values: list[float | int]) -> float | int | None:
    if not values:
        return None
    if metric_direction(metric, 1) == "improved":
        return min(values)
    if metric_direction(metric, 1) == "regressed":
        return max(values)
    return max(values)


def worst_paired_delta(metric: str, deltas: list[float | int]) -> float | int | None:
    if not deltas:
        return None
    if metric_direction(metric, 1) == "improved":
        return min(deltas)
    if metric_direction(metric, 1) == "regressed":
        return max(deltas)
    return max(deltas, key=lambda value: abs(value))


def candidate_regression_config_payload(
    config: CampaignConfig,
    *,
    action_overlay: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    initial_directives: Path | None,
    runtime_metrics: Path,
    run_role: str = "candidate",
    settings: CandidateRegressionSettings | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_regression_config",
        "created_at": utc_timestamp(),
        "run_role": run_role,
        "target": config.target,
        "out_dir": str(config.out_dir),
        "modes": list(config.modes),
        "rounds": config.rounds,
        "iters": config.iters,
        "max_seeds": config.max_seeds,
        "seed": config.seed,
        "run_plan_profile": config.run_plan_profile,
        "campaign_plan_profile": config.campaign_plan_profile,
        "round_evaluation": config.round_evaluation,
        "extra_make_vars": list(config.extra_make_vars),
        "initial_directives": str(initial_directives)
        if initial_directives is not None
        else None,
        "action_overlay": str(action_overlay),
        "runtime_metrics": str(runtime_metrics),
        "adapter_artifacts": adapter_artifacts(adapter_results),
        "adapter_metrics": adapter_metric_snapshot(adapter_results),
        "validation_settings": settings.to_json() if settings is not None else None,
        "campaign_manifest_out": str(config.campaign_manifest_out),
        "campaign_evaluation_out": str(config.campaign_evaluation_out),
        "safety": {
            "mainline_modified": False,
            "application": "sandbox_candidate_regression",
        },
    }


def build_candidate_variant_ranking(
    *,
    action_overlay: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    selected_variant_id: str,
    variant_evaluations: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    evaluations = {
        str(evaluation.get("variant_id")): evaluation
        for evaluation in variant_evaluations
        if evaluation.get("variant_id") is not None
    }
    variants = []
    for variant in list_value(action_overlay.get("variants")):
        if not isinstance(variant, dict):
            continue
        variant_id = str(variant.get("variant_id") or "variant")
        evaluation = evaluations.get(variant_id)
        score = variant_materialization_score(variant)
        validation_status = variant.get("validation_status")
        metrics = candidate_metrics
        if evaluation is not None:
            metrics = numeric_variant_metrics(evaluation)
            validation_status = evaluation.get("status")
            score += variant_status_score(str(evaluation.get("status") or "unknown"))
            score += metric_improvement_score(baseline_metrics, metrics)
        elif variant_id == selected_variant_id:
            validation_status = "executed"
            score += metric_improvement_score(baseline_metrics, metrics)
        variants.append(
            {
                **variant,
                "validation_status": validation_status,
                "score": score,
                "metric_delta_summary": metric_change_summary(
                    baseline_metrics,
                    metrics,
                )
                if evaluation is not None or variant_id == selected_variant_id
                else None,
            }
        )
    ranked = sorted(
        variants,
        key=lambda item: (
            -float(number_value(item.get("score")) or 0),
            str(item.get("variant_id")),
        ),
    )
    for index, variant in enumerate(ranked, start=1):
        variant["rank"] = index
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_variant_ranking",
        "created_at": utc_timestamp(),
        "selected_variant_id": selected_variant_id,
        "variants": ranked,
        "top_variant": ranked[0] if ranked else None,
    }


def combined_variant_evaluation(
    *,
    overlay: dict[str, Any],
    metrics: dict[str, float | int],
    baseline_metrics: dict[str, float | int] | None = None,
    campaign_config: CampaignConfig,
    run_config_path: Path,
    runtime_metrics_path: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    extra_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    variant = next(
        (
            item
            for item in list_value(overlay.get("variants"))
            if isinstance(item, dict) and item.get("variant_id") == "combined"
        ),
        {"variant_id": "combined", "variant_type": "combined_actions"},
    )
    return candidate_variant_evaluation_payload(
        variant=variant,
        status="passed",
        metrics=metrics,
        baseline_metrics=baseline_metrics or {},
        artifacts={
            "candidate_regression_config": str(run_config_path),
            "candidate_runtime_metrics": str(runtime_metrics_path),
            "candidate_campaign_manifest": str(campaign_config.campaign_manifest_out),
            "candidate_campaign_evaluation": str(
                campaign_config.campaign_evaluation_out
            ),
            **(extra_artifacts or {}),
        },
        adapter_results=adapter_results,
    )


def candidate_variant_evaluation_payload(
    *,
    variant: dict[str, Any],
    status: str,
    metrics: dict[str, float | int],
    baseline_metrics: dict[str, float | int],
    artifacts: dict[str, str],
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = {
        "variant_id": str(variant.get("variant_id") or "variant"),
        "variant_type": variant.get("variant_type"),
        "action_ids": list_value(variant.get("action_ids")),
        "action_types": list_value(variant.get("action_types")),
        "status": status,
        "metrics": metrics,
        "metric_delta_summary": metric_change_summary(baseline_metrics, metrics)
        if baseline_metrics
        else {},
        "artifacts": artifacts,
        "actions": [
            entry
            for result in adapter_results
            for entry in result.entries
        ],
    }
    if error is not None:
        payload["error"] = error
    return payload


def select_top_variant_evaluation(
    ranking: dict[str, Any],
    variant_evaluations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    top_variant = mapping(ranking.get("top_variant"))
    top_id = top_variant.get("variant_id")
    for evaluation in variant_evaluations:
        if evaluation.get("variant_id") == top_id:
            return evaluation
    return variant_evaluations[0] if variant_evaluations else {}


def numeric_variant_metrics(
    evaluation: dict[str, Any],
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    _merge_numeric_metrics(metrics, mapping(evaluation.get("metrics")))
    return metrics


def variant_status_score(status: str) -> float:
    if status in {"passed", "ok"}:
        return 100.0
    if status == "not_run":
        return -100.0
    if status in {"error", "failed"}:
        return -1000.0
    return 0.0


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
    summary = metric_threshold_summary(
        baseline_metrics,
        candidate_metrics,
        thresholds,
        stability_summary=stability_summary,
    )
    promotion_status = "ready_for_review" if summary["passes_thresholds"] else "hold"
    action_effects = (
        list_value(mapping(action_effect_report).get("actions"))
        if action_effect_report
        else []
    )
    pruning = build_action_pruning_summary(
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        promotion_status=promotion_status,
        action_effect_report=action_effect_report or {},
    )
    action_effect = mapping(action_effect_report)
    gap_actionability = mapping(gap_actionability_report)
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_promotion_package",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "promotion_status": promotion_status,
        "top_variant": ranking.get("top_variant"),
        "threshold_summary": summary,
        "stability_summary": stability_summary or {},
        "plugin_registry": action_effect.get("plugin_registry")
        or gap_actionability.get("plugin_registry")
        or {},
        "plugin_validation": action_effect.get("plugin_validation")
        or gap_actionability.get("plugin_validation")
        or {},
        "plugin_provenance": action_effect.get("plugin_provenance")
        or gap_actionability.get("plugin_provenance")
        or {},
        "action_effect_summary": action_effect.get("summary") or {},
        "gap_actionability_summary": gap_actionability.get("summary") or {},
        "gap_actionability_minimal_proposal": gap_actionability_minimal_proposal
        or {},
        "action_effects": action_effects,
        "effective_actions": pruning["effective_actions"],
        "neutral_actions": pruning["neutral_actions"],
        "harmful_actions": pruning["harmful_actions"],
        "recommended_promotion_actions": pruning["recommended_promotion_actions"],
        "minimal_promotion_candidate": pruning["minimal_promotion_candidate"],
        "action_pruning_summary": pruning["summary"],
        "evidence_refs": list_value(proposal.get("evidence_refs")),
        "safety": {
            "mainline_modified": False,
            "promotion_mode": "review_artifact_only",
            "sandbox_dir": patch.get("sandbox_dir"),
        },
        "candidate_metrics": candidate_metrics,
        "baseline_metrics": baseline_metrics,
    }


def build_action_pruning_summary(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    promotion_status: str,
    action_effect_report: dict[str, Any],
) -> dict[str, Any]:
    actions = [
        action
        for action in list_value(mapping(action_effect_report).get("actions"))
        if isinstance(action, dict)
    ]
    effective_actions = [
        promotion_action_effect_summary(action)
        for action in actions
        if preferred_action_effect_status(action) == "improved"
    ]
    harmful_actions = [
        promotion_action_effect_summary(action)
        for action in actions
        if preferred_action_effect_status(action) == "regressed"
    ]
    neutral_actions = [
        promotion_action_effect_summary(action)
        for action in actions
        if preferred_action_effect_status(action) not in {"improved", "regressed"}
    ]
    effective_action_ids = [
        str(action["action_id"])
        for action in effective_actions
        if action.get("action_id") is not None
    ]
    recommended_actions = proposal_actions_by_id(proposal, effective_action_ids)
    minimal_variant = find_matching_variant(
        mapping(action_effect_report),
        effective_action_ids,
    )
    minimal_validation_status = "not_recommended"
    if effective_action_ids:
        minimal_validation_status = (
            "validated"
            if minimal_variant
            and str(minimal_variant.get("status") or "") in {"passed", "ok"}
            else "requires_validation"
        )
    minimal_status = (
        "ready_for_review"
        if effective_action_ids
        and minimal_validation_status == "validated"
        else "hold"
    )
    minimal_candidate = {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_minimal_promotion_candidate",
        "status": minimal_status,
        "validation_status": minimal_validation_status,
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "attribution_basis": "standalone_effect_status_preferred",
        "source_promotion_status": promotion_status,
        "action_ids": effective_action_ids,
        "action_count": len(effective_action_ids),
        "actions": recommended_actions,
        "dropped_action_ids": [
            str(action["action_id"])
            for action in (*neutral_actions, *harmful_actions)
            if action.get("action_id") is not None
        ],
        "harmful_action_ids": [
            str(action["action_id"])
            for action in harmful_actions
            if action.get("action_id") is not None
        ],
        "validated_by_variant_id": (
            minimal_variant.get("variant_id") if minimal_variant else None
        ),
        "requires_validation": minimal_validation_status != "validated",
    }
    return {
        "summary": {
            "attribution_basis": "standalone_effect_status_preferred",
            "source_promotion_status": promotion_status,
            "effective_action_count": len(effective_actions),
            "neutral_action_count": len(neutral_actions),
            "harmful_action_count": len(harmful_actions),
            "recommended_action_count": len(recommended_actions),
            "minimal_action_count": len(effective_action_ids),
            "minimal_validation_status": minimal_validation_status,
            "minimal_status": minimal_status,
        },
        "effective_actions": effective_actions,
        "neutral_actions": neutral_actions,
        "harmful_actions": harmful_actions,
        "recommended_promotion_actions": recommended_actions,
        "minimal_promotion_candidate": minimal_candidate,
    }


def preferred_action_effect_status(action: dict[str, Any]) -> str:
    status = action.get("standalone_effect_status")
    if status is None:
        status = action.get("effect_status")
    if status is None:
        status = action.get("aggregate_effect_status")
    return str(status or "neutral")


def promotion_action_effect_summary(action: dict[str, Any]) -> dict[str, Any]:
    supporting_variants = [
        variant
        for variant in list_value(action.get("supporting_variants"))
        if isinstance(variant, dict)
    ]
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "effect_status": action.get("effect_status"),
        "standalone_effect_status": action.get("standalone_effect_status"),
        "combined_effect_status": action.get("combined_effect_status"),
        "aggregate_effect_status": action.get("aggregate_effect_status"),
        "preferred_effect_status": preferred_action_effect_status(action),
        "consumed": action.get("consumed"),
        "is_runtime_action": action.get("is_runtime_action"),
        "supporting_variant_ids": [
            variant.get("variant_id")
            for variant in supporting_variants
            if variant.get("variant_id") is not None
        ],
    }


def proposal_actions_by_id(
    proposal: dict[str, Any],
    action_ids: list[str],
) -> list[dict[str, Any]]:
    proposal_actions = [
        action
        for action in list_value(proposal.get("actions"))
        if isinstance(action, dict)
    ]
    by_id = {
        str(action.get("action_id")): action
        for action in proposal_actions
        if action.get("action_id") is not None
    }
    result = []
    for action_id in action_ids:
        action = by_id.get(action_id)
        if action is not None:
            result.append(action)
    return result


def find_matching_variant(
    action_effect_report: dict[str, Any],
    action_ids: list[str],
) -> dict[str, Any]:
    expected = set(action_ids)
    if not expected:
        return {}
    for variant in list_value(action_effect_report.get("variants")):
        if not isinstance(variant, dict):
            continue
        observed = {
            str(action_id)
            for action_id in list_value(variant.get("action_ids"))
            if action_id is not None
        }
        if observed == expected:
            return variant
    return {}


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
        "plugin_registry": registry.to_json(),
        "plugin_validation": registry.validation_json(),
        "plugin_provenance": registry.provenance_json(),
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
        "plugin_registry": registry.to_json(),
        "plugin_validation": registry.validation_json(),
        "plugin_provenance": registry.provenance_json(),
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
        "plugin_registry": registry.to_json(),
        "plugin_validation": registry.validation_json(),
        "plugin_provenance": registry.provenance_json(),
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


def metric_threshold_summary(
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    thresholds: CandidateAcceptanceThresholds,
    *,
    stability_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    improved = 0
    regressed = 0
    changed = 0
    gateable_improved = 0
    gateable_regressed = 0
    gateable_unchanged = 0
    informational_changed = 0
    for metric, baseline in baseline_metrics.items():
        if metric not in candidate_metrics:
            continue
        delta = candidate_metrics[metric] - baseline
        direction = metric_direction(metric, delta)
        gates_acceptance = metric_gates_acceptance(metric)
        if direction == "improved":
            improved += 1
            if gates_acceptance:
                gateable_improved += 1
        elif direction == "regressed":
            regressed += 1
            if gates_acceptance:
                gateable_regressed += 1
        elif direction == "changed":
            changed += 1
            if not gates_acceptance:
                informational_changed += 1
        elif direction == "unchanged" and gates_acceptance:
            gateable_unchanged += 1
    flaky_metric_count = int(
        number_value(mapping(stability_summary).get("flaky_metric_count")) or 0
    )
    return {
        "improved_metric_count": improved,
        "regressed_metric_count": regressed,
        "changed_metric_count": changed,
        "gateable_improved_metric_count": gateable_improved,
        "gateable_regressed_metric_count": gateable_regressed,
        "gateable_unchanged_metric_count": gateable_unchanged,
        "informational_changed_metric_count": informational_changed,
        "max_regressed_metric_count": thresholds.max_regressed_metric_count,
        "min_improved_metric_count": thresholds.min_improved_metric_count,
        "flaky_metric_count": flaky_metric_count,
        "max_flaky_metric_count": thresholds.max_flaky_metric_count,
        "passes_thresholds": (
            gateable_regressed <= thresholds.max_regressed_metric_count
            and gateable_improved >= thresholds.min_improved_metric_count
            and flaky_metric_count <= thresholds.max_flaky_metric_count
        ),
    }


def metric_change_summary(
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
) -> dict[str, int]:
    improved = 0
    regressed = 0
    changed = 0
    gateable_improved = 0
    gateable_regressed = 0
    gateable_unchanged = 0
    informational_changed = 0
    comparable = 0
    for metric, baseline in baseline_metrics.items():
        if metric not in candidate_metrics:
            continue
        comparable += 1
        delta = candidate_metrics[metric] - baseline
        direction = metric_direction(metric, delta)
        gates_acceptance = metric_gates_acceptance(metric)
        if direction == "improved":
            improved += 1
            if gates_acceptance:
                gateable_improved += 1
        elif direction == "regressed":
            regressed += 1
            if gates_acceptance:
                gateable_regressed += 1
        elif direction == "changed":
            changed += 1
            if not gates_acceptance:
                informational_changed += 1
        elif direction == "unchanged" and gates_acceptance:
            gateable_unchanged += 1
    return {
        "comparable_metric_count": comparable,
        "improved_metric_count": improved,
        "regressed_metric_count": regressed,
        "changed_metric_count": changed,
        "gateable_improved_metric_count": gateable_improved,
        "gateable_regressed_metric_count": gateable_regressed,
        "gateable_unchanged_metric_count": gateable_unchanged,
        "informational_changed_metric_count": informational_changed,
    }


def metric_improvement_score(
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
) -> float:
    score = 0.0
    for metric, baseline in baseline_metrics.items():
        if metric not in candidate_metrics:
            continue
        delta = candidate_metrics[metric] - baseline
        direction = metric_direction(metric, delta)
        if direction == "improved":
            score += 10.0
        elif direction == "regressed":
            score -= 20.0
        elif direction == "changed":
            score += 1.0
    return score


def variant_materialization_score(variant: dict[str, Any]) -> float:
    return float(len(list_value(variant.get("action_ids"))))


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
