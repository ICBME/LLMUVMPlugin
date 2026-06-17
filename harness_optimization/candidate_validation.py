from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Protocol

from .candidate_execution import (
    CandidateAcceptanceThresholds,
    CandidateActionAdapterResult,
    adapter_artifacts,
    adapter_metric_snapshot,
    build_candidate_variants,
)
from .io import (
    artifact_path,
    campaign_cwd,
    optional_artifact,
    optional_existing_artifact,
    read_json_object,
)
from .plugins import HarnessGapActionabilityContext, HarnessPluginRegistry
from .records import list_value, mapping
from .runtime import read_runtime_metrics, runtime_metric_snapshot
from .rules import (
    metric_direction,
    metric_gates_acceptance,
    number_value,
    safe_slug,
    validate_harness_optimization_proposal,
)


CANDIDATE_ACTION_EFFECT_REPORT_KIND = (
    "harness_optimization.candidate_action_effect_report"
)
CANDIDATE_GAP_ACTIONABILITY_REPORT_KIND = (
    "harness_optimization.candidate_gap_actionability_report"
)
CANDIDATE_GAP_ACTIONABILITY_MINIMAL_PROPOSAL_SOURCE = (
    "candidate_gap_actionability_report"
)
CANDIDATE_EVALUATION_KIND = "harness_optimization.candidate_evaluation"
CANDIDATE_VARIANT_RANKING_KIND = "harness_optimization.candidate_variant_ranking"
CANDIDATE_PROMOTION_PACKAGE_KIND = (
    "harness_optimization.candidate_promotion_package"
)
MINIMAL_PROMOTION_CANDIDATE_KIND = (
    "harness_optimization.minimal_promotion_candidate"
)


class CandidateValidationCampaignOutputs(Protocol):
    campaign_manifest_out: Path | None
    campaign_evaluation_out: Path | None


CandidateVariantBuilder = Callable[
    [tuple[CandidateActionAdapterResult, ...]],
    list[dict[str, Any]],
]


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


def build_candidate_variant_ranking(
    *,
    action_overlay: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    selected_variant_id: str,
    variant_evaluations: tuple[dict[str, Any], ...] = (),
    kind: str = CANDIDATE_VARIANT_RANKING_KIND,
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
        "kind": kind,
        "created_at": _utc_timestamp(),
        "selected_variant_id": selected_variant_id,
        "variants": ranked,
        "top_variant": ranked[0] if ranked else None,
    }


def combined_variant_evaluation(
    *,
    overlay: dict[str, Any],
    metrics: dict[str, float | int],
    baseline_metrics: dict[str, float | int] | None = None,
    campaign_config: CandidateValidationCampaignOutputs,
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
            **_optional_artifact_path(
                "candidate_campaign_manifest",
                campaign_config.campaign_manifest_out,
            ),
            **_optional_artifact_path(
                "candidate_campaign_evaluation",
                campaign_config.campaign_evaluation_out,
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
        "actions": [entry for result in adapter_results for entry in result.entries],
    }
    if error is not None:
        payload["error"] = error
    return payload


def build_candidate_not_run_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    patch: dict[str, Any],
    candidate_manifest: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    acceptance_thresholds: CandidateAcceptanceThresholds,
    plugin_provenance: dict[str, Any],
    plugin_provenance_path: Path,
    run_config_path: Path,
    overlay_path: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    directives_path: Path | None,
    runtime_metrics_path: Path | None,
    reason: str,
    source: str,
    matched_baseline_enabled: bool = False,
    kind: str = CANDIDATE_EVALUATION_KIND,
    variant_builder: CandidateVariantBuilder = build_candidate_variants,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "source": source,
        "status": "not_run",
        "application_status": patch.get("status"),
        "baseline_source": "task_snapshot",
        "baseline_metrics": baseline_metrics,
        "source_baseline_metrics": baseline_metrics,
        "matched_baseline_metrics": None,
        "candidate_metrics": dict(baseline_metrics),
        "acceptance_thresholds": acceptance_thresholds.to_json(),
        "plugin_validation": mapping(plugin_provenance.get("validation")),
        "plugin_provenance": plugin_provenance,
        "reason": reason,
        "artifacts": {
            "candidate_regression_config": str(run_config_path),
            "candidate_action_overlay": str(overlay_path),
            "candidate_plugin_provenance": str(plugin_provenance_path),
            **adapter_artifacts(adapter_results),
            **optional_artifact(
                "candidate_mutation_directives",
                directives_path,
            ),
            **optional_existing_artifact(
                "candidate_runtime_metrics",
                runtime_metrics_path,
            ),
        },
        "summary": {
            "matched_baseline": {
                "enabled": matched_baseline_enabled,
                "status": "skipped",
                "reason": reason,
                "baseline_source": "task_snapshot",
            },
            **adapter_metric_snapshot(adapter_results),
            "candidate_variant_count": len(variant_builder(adapter_results)),
        },
    }


def build_candidate_error_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    acceptance_thresholds: CandidateAcceptanceThresholds,
    plugin_provenance: dict[str, Any],
    plugin_provenance_path: Path,
    run_config_path: Path,
    overlay_path: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    directives_path: Path | None,
    runtime_metrics_path: Path | None,
    error: BaseException,
    source: str,
    baseline_source: str = "task_snapshot",
    source_baseline_metrics: dict[str, float | int] | None = None,
    error_context: str = "candidate_campaign",
    matched_baseline_artifacts: dict[str, str] | None = None,
    matched_baseline_summary: dict[str, Any] | None = None,
    kind: str = CANDIDATE_EVALUATION_KIND,
    variant_builder: CandidateVariantBuilder = build_candidate_variants,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "source": source,
        "status": "error",
        "baseline_source": baseline_source,
        "baseline_metrics": baseline_metrics,
        "source_baseline_metrics": source_baseline_metrics or baseline_metrics,
        "matched_baseline_metrics": (
            baseline_metrics if baseline_source == "matched_noop_rerun" else None
        ),
        "candidate_metrics": {},
        "acceptance_thresholds": acceptance_thresholds.to_json(),
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
            **optional_artifact(
                "candidate_mutation_directives",
                directives_path,
            ),
            **optional_existing_artifact(
                "candidate_runtime_metrics",
                runtime_metrics_path,
            ),
        },
        "summary": {
            "matched_baseline": matched_baseline_summary
            or {
                "enabled": False,
                "status": "disabled",
                "baseline_source": "task_snapshot",
            },
            **adapter_metric_snapshot(adapter_results),
            "candidate_variant_count": len(variant_builder(adapter_results)),
        },
    }


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
    kind: str = CANDIDATE_PROMOTION_PACKAGE_KIND,
    minimal_candidate_kind: str = MINIMAL_PROMOTION_CANDIDATE_KIND,
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
        minimal_candidate_kind=minimal_candidate_kind,
    )
    action_effect = mapping(action_effect_report)
    gap_actionability = mapping(gap_actionability_report)
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
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


def candidate_metric_snapshot(
    campaign_manifest: dict[str, Any],
    campaign_evaluation: dict[str, Any],
    *,
    cwd: Path | None,
    harness_evaluation_role: str = "harness_evaluation",
    runtime_metrics_role: str = "harness_runtime_metrics",
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for source in (
        mapping(campaign_evaluation.get("summary")),
        mapping(mapping(campaign_evaluation.get("harness_trace")).get("summary")),
    ):
        _merge_numeric_metrics(metrics, source)
    harness_evaluation = _candidate_harness_evaluation(
        campaign_evaluation,
        cwd=cwd,
        harness_evaluation_role=harness_evaluation_role,
    )
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("summary")))
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("trace_quality")))
    _merge_campaign_round_metrics(metrics, campaign_manifest)
    _merge_campaign_runtime_metrics(
        metrics,
        campaign_manifest,
        cwd=cwd,
        runtime_metrics_role=runtime_metrics_role,
    )
    return metrics


def build_candidate_action_effect_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    variant_evaluations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    runtime_action_types: Iterable[str] = (),
    runtime_metrics_reader: Callable[[Path | None], dict[str, Any]] = read_runtime_metrics,
    runtime_metrics_artifact_role: str = "candidate_runtime_metrics",
    plugin_registry: dict[str, Any] | None = None,
    plugin_validation: dict[str, Any] | None = None,
    plugin_provenance: dict[str, Any] | None = None,
    kind: str = CANDIDATE_ACTION_EFFECT_REPORT_KIND,
) -> dict[str, Any]:
    variants = []
    consumed_action_ids: set[str] = set()
    runtime_action_ids: set[str] = set()
    runtime_types = {str(item) for item in runtime_action_types if item is not None}
    for evaluation in variant_evaluations:
        runtime_payload = runtime_metrics_reader(
            artifact_path(
                mapping(evaluation.get("artifacts")),
                runtime_metrics_artifact_role,
                None,
            )
        )
        actions = build_variant_action_effects(
            evaluation=evaluation,
            runtime_payload=runtime_payload,
            baseline_metrics=baseline_metrics,
            runtime_action_types=runtime_types,
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
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "plugin_registry": plugin_registry or {},
        "plugin_validation": plugin_validation or {},
        "plugin_provenance": plugin_provenance or {},
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
    plugin_registry: HarnessPluginRegistry,
    kind: str = CANDIDATE_GAP_ACTIONABILITY_REPORT_KIND,
    coverage_summary_role: str = "coverage_summary",
    gap_summary_key: str = "rtl_gap_summary",
    top_gaps_key: str = "top_gaps",
    actionability_blockers: tuple[str, ...] = (
        "requires_mmio_write_surface",
        "requires_internal_state_surface",
    ),
    mmio_readback_action_type: str = "mmio_readback",
) -> dict[str, Any]:
    baseline_summary_path = latest_campaign_artifact_path(
        baseline_campaign_manifest,
        role=coverage_summary_role,
    )
    candidate_summary_path = latest_campaign_artifact_path(
        candidate_campaign_manifest,
        role=coverage_summary_role,
    )
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
        plugin_registry.classify_gap_actionability(gap, gap_context)
        for gap in list_value(
            mapping(candidate_summary.get(gap_summary_key)).get(top_gaps_key)
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
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "baseline_coverage_summary": (
            str(baseline_summary_path) if baseline_summary_path else None
        ),
        "candidate_coverage_summary": (
            str(candidate_summary_path) if candidate_summary_path else None
        ),
        "baseline_uncovered_line_count": baseline_uncovered,
        "candidate_uncovered_line_count": candidate_uncovered,
        "uncovered_line_delta": delta,
        "remaining_gaps": gaps,
        "plugin_registry": plugin_registry.to_json(),
        "plugin_validation": plugin_registry.validation_json(),
        "plugin_provenance": plugin_registry.provenance_json(),
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
                gap.get("actionability") in set(actionability_blockers)
                for gap in gaps
            ),
            "has_mmio_readback_targets": any(
                gap.get("recommended_action_type") == mmio_readback_action_type
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
    plugin_registry: HarnessPluginRegistry,
    proposal_kind: str,
    proposal_source: str = CANDIDATE_GAP_ACTIONABILITY_MINIMAL_PROPOSAL_SOURCE,
    proposal_id_suffix: str = "gap_actionability_minimal",
) -> dict[str, Any]:
    safe_action_types = set(plugin_registry.safe_sandbox_action_types())
    allowed_action_types = set(plugin_registry.allowed_action_types())
    payload_required_types = set(plugin_registry.dsl_payload_action_types())
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
        payload_errors = plugin_registry.action_payload_errors(
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
        "kind": proposal_kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": (
            f"{candidate_manifest.get('candidate_id') or 'candidate'}:"
            f"{proposal_id_suffix}"
        ),
        "status": status,
        "source": proposal_source,
        "source_candidate_id": candidate_manifest.get("candidate_id"),
        "actions": actions,
        "evidence_refs": proposal_refs,
        "skipped_recommendations": skipped,
        "plugin_registry": plugin_registry.to_json(),
        "plugin_validation": plugin_registry.validation_json(),
        "plugin_provenance": plugin_registry.provenance_json(),
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
        proposal_kind=proposal_kind,
        plugin_registry=plugin_registry,
    )
    return minimal


def build_action_pruning_summary(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    candidate_manifest: dict[str, Any],
    promotion_status: str,
    action_effect_report: dict[str, Any],
    minimal_candidate_kind: str = MINIMAL_PROMOTION_CANDIDATE_KIND,
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
        "kind": minimal_candidate_kind,
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


def build_variant_action_effects(
    *,
    evaluation: dict[str, Any],
    runtime_payload: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    runtime_action_types: Iterable[str] = (),
) -> list[dict[str, Any]]:
    runtime_types = {str(item) for item in runtime_action_types if item is not None}
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
        is_runtime_action = action_type in runtime_types
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
    if int(number_value(delta_summary.get("gateable_regressed_metric_count")) or 0) > 0:
        return "regressed"
    if int(number_value(delta_summary.get("gateable_improved_metric_count")) or 0) > 0:
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


def _merge_numeric_metrics(
    target: dict[str, float | int],
    values: dict[str, Any],
) -> None:
    for key, value in values.items():
        number = number_value(value)
        if number is not None:
            target[str(key)] = number


def _optional_artifact_path(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}


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


def latest_campaign_artifact_path(
    campaign_manifest: dict[str, Any],
    *,
    role: str,
) -> Path | None:
    result: Path | None = None
    cwd = campaign_cwd(campaign_manifest, None)
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            path = artifact_path(mapping(round_item.get("artifacts")), role, cwd)
            if path is not None:
                result = path
    return result


def _candidate_harness_evaluation(
    campaign_evaluation: dict[str, Any],
    *,
    cwd: Path | None,
    harness_evaluation_role: str,
) -> dict[str, Any]:
    artifacts = mapping(mapping(campaign_evaluation.get("harness_trace")).get("artifacts"))
    path = artifact_path(artifacts, harness_evaluation_role, cwd)
    return read_json_object(path)


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
    runtime_metrics_role: str,
) -> None:
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            path = artifact_path(
                mapping(round_item.get("artifacts")),
                runtime_metrics_role,
                cwd,
            )
            _merge_numeric_metrics(metrics, runtime_metric_snapshot(path))


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


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


__all__ = [
    "CANDIDATE_ACTION_EFFECT_REPORT_KIND",
    "CANDIDATE_EVALUATION_KIND",
    "CANDIDATE_GAP_ACTIONABILITY_MINIMAL_PROPOSAL_SOURCE",
    "CANDIDATE_GAP_ACTIONABILITY_REPORT_KIND",
    "CANDIDATE_PROMOTION_PACKAGE_KIND",
    "CANDIDATE_VARIANT_RANKING_KIND",
    "MINIMAL_PROMOTION_CANDIDATE_KIND",
    "action_effect_status_counts",
    "aggregate_metric_snapshots",
    "aggregate_metric_value",
    "aggregate_action_effects",
    "build_action_pruning_summary",
    "build_candidate_action_effect_report",
    "build_candidate_error_report",
    "build_candidate_gap_actionability_report",
    "build_candidate_not_run_report",
    "build_candidate_promotion_package",
    "build_candidate_variant_ranking",
    "build_gap_actionability_minimal_candidate_proposal",
    "build_variant_action_effects",
    "candidate_metric_snapshot",
    "candidate_variant_evaluation_payload",
    "classify_action_effect",
    "combined_variant_evaluation",
    "find_matching_variant",
    "merge_action_effect_status",
    "merge_optional_action_effect_status",
    "metric_change_summary",
    "metric_improvement_score",
    "metric_threshold_summary",
    "metric_variance",
    "numeric_variant_metrics",
    "paired_metric_stability",
    "paired_validation_run_payload",
    "preferred_action_effect_status",
    "promotion_action_effect_summary",
    "proposal_actions_by_id",
    "runtime_action_metric_map",
    "runtime_section_metric_map",
    "select_top_variant_evaluation",
    "variant_materialization_score",
    "variant_status_score",
    "worst_metric_value",
    "worst_paired_delta",
]
