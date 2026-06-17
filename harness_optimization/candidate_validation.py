from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .candidate_execution import (
    CandidateAcceptanceThresholds,
    CandidateActionAdapterResult,
)
from .records import list_value, mapping
from .rules import metric_direction, metric_gates_acceptance, number_value


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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CANDIDATE_PROMOTION_PACKAGE_KIND",
    "CANDIDATE_VARIANT_RANKING_KIND",
    "MINIMAL_PROMOTION_CANDIDATE_KIND",
    "aggregate_metric_snapshots",
    "aggregate_metric_value",
    "build_action_pruning_summary",
    "build_candidate_promotion_package",
    "build_candidate_variant_ranking",
    "candidate_variant_evaluation_payload",
    "combined_variant_evaluation",
    "find_matching_variant",
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
    "select_top_variant_evaluation",
    "variant_materialization_score",
    "variant_status_score",
    "worst_metric_value",
    "worst_paired_delta",
]
