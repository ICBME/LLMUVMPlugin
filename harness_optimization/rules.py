from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .action_dsl import (
    BUILTIN_DSL_PAYLOAD_ACTION_TYPES,
    SCOREBOARD_CHECK_MODES,
    builtin_action_payload_validators as _builtin_action_payload_validators,
    builtin_action_plugin_registry,
    builtin_safe_action_dsl_schema as _builtin_safe_action_dsl_schema,
    coverage_feedback_tuning_payload_errors,
    mmio_readback_payload_errors,
    replay_probe_payload_errors,
    scoreboard_check_payload_errors,
)
from .plugins import HarnessPluginRegistry
from .records import list_value, mapping


PayloadValidator = Callable[[dict[str, Any], str], list[dict[str, str]]]

PROPOSAL_STATUS_VALUES = ("no_op", "proposed")
CANDIDATE_EVALUATION_STATUS_VALUES = ("not_run", "passed", "ok", "failed", "error")
DEFAULT_ACCEPTED_CANDIDATE_STATUSES = ("ok", "passed")

LOWER_IS_BETTER_METRICS = {
    "failed_record_count",
    "hanging_span_count",
    "malformed_event_line_count",
    "missing_span_id_count",
    "orphan_final_count",
    "orphan_span_count",
    "scoreboard_check_enforced_failure_count",
    "scoreboard_check_failed_count",
    "uncovered_line_count",
}

HIGHER_IS_BETTER_METRICS = {
    "covered_line_count",
    "coverage_percent",
}

INFORMATIONAL_METRICS = {
    "case_count",
    "directive_count",
    "llm_sample_count",
    "record_count",
    "round_count",
}

def optimizer_proposal_schema_hint(
    task: dict[str, Any],
    *,
    proposal_kind: str,
    default_allowed_action_types: tuple[str, ...] | list[str] = (),
    default_safe_action_types: tuple[str, ...] | list[str] = (),
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    constraints = mapping(task.get("constraints"))
    allowed_action_types = list_value(constraints.get("allowed_action_types"))
    if not allowed_action_types:
        if default_allowed_action_types:
            allowed_action_types = list(default_allowed_action_types)
        elif plugin_registry is not None:
            allowed_action_types = list(plugin_registry.allowed_action_types())
    safe_action_types = list_value(constraints.get("safe_sandbox_action_types"))
    if not safe_action_types:
        if default_safe_action_types:
            safe_action_types = list(default_safe_action_types)
        elif plugin_registry is not None:
            safe_action_types = list(plugin_registry.safe_sandbox_action_types())
    action_dsl = mapping(constraints.get("safe_action_dsl")) or safe_action_dsl_schema(
        plugin_registry
    )
    return {
        "schema_version": 1,
        "kind": proposal_kind,
        "required_top_level_fields": [
            "schema_version",
            "kind",
            "proposal_id",
            "status",
            "actions",
            "evidence_refs",
        ],
        "status_values": list(PROPOSAL_STATUS_VALUES),
        "allowed_action_types": allowed_action_types,
        "safe_sandbox_action_types": safe_action_types,
        "safe_action_dsl": action_dsl,
        "evidence_ref_fields": ["span_id", "case_id", "directive_id", "connector"],
    }


def safe_action_dsl_schema(
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    if plugin_registry is not None:
        return plugin_registry.safe_action_dsl_schema()
    return _builtin_safe_action_dsl_schema()


def builtin_safe_action_dsl_schema() -> dict[str, Any]:
    return _builtin_safe_action_dsl_schema()


def builtin_action_payload_validators() -> dict[str, PayloadValidator]:
    return _builtin_action_payload_validators()


def validate_harness_optimization_proposal(
    proposal: dict[str, Any],
    *,
    task: dict[str, Any],
    proposal_kind: str,
    plugin_registry: HarnessPluginRegistry | None = None,
    default_allowed_action_types: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if proposal.get("kind") != proposal_kind:
        errors.append({"path": "kind", "message": f"expected {proposal_kind!r}"})
    if proposal.get("schema_version") != 1:
        errors.append(
            {"path": "schema_version", "message": "expected schema_version 1"}
        )
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        errors.append({"path": "proposal_id", "message": "expected non-empty string"})
    status = proposal.get("status")
    if status not in PROPOSAL_STATUS_VALUES:
        errors.append(
            {
                "path": "status",
                "message": "expected 'no_op' or 'proposed'",
            }
        )
    actions = proposal.get("actions")
    if not isinstance(actions, list):
        errors.append({"path": "actions", "message": "expected list"})
        actions = []
    if status == "proposed" and not actions:
        errors.append(
            {
                "path": "actions",
                "message": "proposed status requires at least one action",
            }
        )

    allowed = set(
        list_value(mapping(task.get("constraints")).get("allowed_action_types"))
    )
    if not allowed:
        if default_allowed_action_types:
            allowed = set(default_allowed_action_types)
        elif plugin_registry is not None:
            allowed = set(plugin_registry.allowed_action_types())
    if plugin_registry is not None:
        dsl_payload_action_types = set(plugin_registry.dsl_payload_action_types())
    else:
        dsl_payload_action_types = set(
            builtin_action_plugin_registry().dsl_payload_action_types()
        )
    evidence_index = mapping(task.get("evidence_index"))
    proposal_refs_raw = proposal.get("evidence_refs")
    if proposal_refs_raw is not None and not isinstance(proposal_refs_raw, list):
        errors.append({"path": "evidence_refs", "message": "expected list"})
    proposal_refs = list_value(proposal_refs_raw)
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(
                {"path": f"actions[{index}]", "message": "expected action object"}
            )
            continue
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            errors.append(
                {
                    "path": f"actions[{index}].action_id",
                    "message": "expected non-empty string",
                }
            )
        action_type = action.get("action_type")
        if action_type not in allowed:
            errors.append(
                {
                    "path": f"actions[{index}].action_type",
                    "message": f"unsupported action type {action_type!r}",
                }
            )
        payload = action.get("payload")
        if action_type in dsl_payload_action_types and not isinstance(payload, dict):
            errors.append(
                {
                    "path": f"actions[{index}].payload",
                    "message": f"{action_type} payload is required",
                }
            )
        elif payload is not None and not isinstance(payload, dict):
            errors.append(
                {
                    "path": f"actions[{index}].payload",
                    "message": "expected payload object",
                }
            )
        elif isinstance(payload, dict):
            errors.extend(
                action_payload_errors(
                    str(action_type),
                    payload,
                    path=f"actions[{index}].payload",
                    plugin_registry=plugin_registry,
                )
            )
        action_refs_raw = action.get("evidence_refs")
        if action_refs_raw is not None and not isinstance(action_refs_raw, list):
            errors.append(
                {
                    "path": f"actions[{index}].evidence_refs",
                    "message": "expected list",
                }
            )
            action_refs = []
        else:
            action_refs = list_value(action_refs_raw) or proposal_refs
        if status == "proposed" and not action_refs:
            errors.append(
                {
                    "path": f"actions[{index}].evidence_refs",
                    "message": "proposed actions require evidence_refs",
                }
            )
        for ref_index, ref in enumerate(action_refs):
            ref_error = evidence_ref_error(ref, evidence_index)
            if ref_error is not None:
                errors.append(
                    {
                        "path": f"actions[{index}].evidence_refs[{ref_index}]",
                        "message": ref_error,
                    }
                )

    return {
        "valid": not errors,
        "error_count": len(errors),
        "errors": errors,
        "allowed_action_types": sorted(allowed),
    }


def action_payload_errors(
    action_type: str,
    payload: dict[str, Any],
    *,
    path: str,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> list[dict[str, str]]:
    if plugin_registry is not None:
        return plugin_registry.action_payload_errors(action_type, payload, path=path)
    return builtin_action_plugin_registry().action_payload_errors(
        action_type,
        payload,
        path=path,
    )


def evidence_index_for(harness_evaluation: dict[str, Any]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {
        "span_ids": set(),
        "case_ids": set(),
        "directive_ids": set(),
        "connectors": set(),
    }
    for section in ("failed_records", "slowest_records"):
        for record in list_value(harness_evaluation.get(section)):
            collect_record_evidence(record, values)
    for cluster in list_value(harness_evaluation.get("failure_clusters")):
        connector = cluster.get("connector")
        if connector is not None:
            values["connectors"].add(str(connector))
        for record in list_value(cluster.get("examples")):
            collect_record_evidence(record, values)
    for item in list_value(harness_evaluation.get("case_summary")):
        case_id = item.get("case_id")
        if case_id is not None:
            values["case_ids"].add(str(case_id))
    for item in list_value(harness_evaluation.get("directive_summary")):
        directive_id = item.get("directive_id")
        if directive_id is not None:
            values["directive_ids"].add(str(directive_id))
    return {key: sorted(items) for key, items in values.items()}


def collect_record_evidence(record: Any, values: dict[str, set[str]]) -> None:
    if not isinstance(record, dict):
        return
    span_id = record.get("span_id")
    if span_id is not None:
        values["span_ids"].add(str(span_id))
    case_id = record.get("case_id")
    if case_id is not None:
        values["case_ids"].add(str(case_id))
    directive_id = record.get("directive_id")
    if directive_id is not None:
        values["directive_ids"].add(str(directive_id))
    connector = record.get("connector")
    if connector is not None:
        values["connectors"].add(str(connector))


def evidence_ref_error(ref: Any, evidence_index: dict[str, Any]) -> str | None:
    if not isinstance(ref, dict):
        return "expected evidence ref object"
    allowed_fields = {
        "span_id": "span_ids",
        "case_id": "case_ids",
        "directive_id": "directive_ids",
        "connector": "connectors",
    }
    present = [
        (field, str(ref[field]))
        for field in allowed_fields
        if ref.get(field) is not None
    ]
    if not present:
        return "expected one of span_id, case_id, directive_id, or connector"
    for field, value in present:
        known = set(str(item) for item in list_value(evidence_index.get(allowed_fields[field])))
        if known and value not in known:
            return f"unknown {field} {value!r}"
    return None


def final_decision_thresholds(
    candidate_evaluation: dict[str, Any],
    metric_delta: dict[str, Any],
    *,
    default_accepted_candidate_statuses: tuple[str, ...] = DEFAULT_ACCEPTED_CANDIDATE_STATUSES,
) -> dict[str, Any]:
    raw = mapping(candidate_evaluation.get("acceptance_thresholds")) or mapping(
        metric_delta.get("acceptance_thresholds")
    )
    statuses = [
        str(item)
        for item in list_value(raw.get("accepted_candidate_statuses"))
        if item is not None
    ]
    return {
        "max_regressed_metric_count": int_value(
            raw.get("max_regressed_metric_count")
            if "max_regressed_metric_count" in raw
            else 0
        ),
        "min_improved_metric_count": int_value(
            raw.get("min_improved_metric_count")
            if "min_improved_metric_count" in raw
            else 0
        ),
        "max_flaky_metric_count": int_value(
            raw.get("max_flaky_metric_count")
            if "max_flaky_metric_count" in raw
            else 0
        ),
        "accepted_candidate_statuses": statuses
        or list(default_accepted_candidate_statuses),
    }


def normalize_candidate_evaluation(
    value: Any,
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
    candidate_evaluation_kind: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return candidate_evaluation_error(
            task=task,
            proposal=proposal,
            source=source,
            error_type="TypeError",
            message="candidate evaluation backend returned a non-object",
            candidate_evaluation_kind=candidate_evaluation_kind,
        )
    evaluation = dict(value)
    evaluation.setdefault("schema_version", 1)
    evaluation.setdefault("kind", candidate_evaluation_kind)
    evaluation.setdefault("created_at", _utc_timestamp())
    evaluation.setdefault("target", task.get("target"))
    evaluation.setdefault("run_id", task.get("run_id"))
    evaluation.setdefault("proposal_id", proposal.get("proposal_id"))
    evaluation.setdefault("source", source)
    errors = list_value(evaluation.get("schema_errors"))
    if evaluation.get("kind") != candidate_evaluation_kind:
        errors.append(
            {
                "path": "kind",
                "message": f"expected {candidate_evaluation_kind!r}",
            }
        )
    if evaluation.get("schema_version") != 1:
        errors.append({"path": "schema_version", "message": "expected 1"})
    if evaluation.get("status") not in CANDIDATE_EVALUATION_STATUS_VALUES:
        errors.append(
            {
                "path": "status",
                "message": (
                    "expected one of not_run, passed, ok, failed, error"
                ),
            }
        )
    if not isinstance(evaluation.get("baseline_metrics"), dict):
        evaluation["baseline_metrics"] = baseline_metric_snapshot(task)
    if not isinstance(evaluation.get("candidate_metrics"), dict):
        evaluation["candidate_metrics"] = {}
    if errors:
        evaluation["status"] = "error"
        evaluation["schema_errors"] = errors
    return evaluation


def candidate_evaluation_error(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
    error_type: str,
    message: str,
    candidate_evaluation_kind: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": candidate_evaluation_kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "source": source,
        "status": "error",
        "baseline_metrics": baseline_metric_snapshot(task),
        "candidate_metrics": {},
        "error": {"type": error_type, "message": message},
    }


def build_candidate_metric_delta(
    *,
    task: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    baseline = numeric_metrics(
        mapping(candidate_evaluation.get("baseline_metrics"))
        or baseline_metric_snapshot(task)
    )
    candidate = numeric_metrics(mapping(candidate_evaluation.get("candidate_metrics")))
    comparisons = []
    for name in sorted(set(baseline) | set(candidate)):
        base_value = baseline.get(name)
        candidate_value = candidate.get(name)
        role = metric_role(name)
        gates_acceptance = metric_gates_acceptance(name)
        if base_value is None or candidate_value is None:
            comparisons.append(
                {
                    "metric": name,
                    "role": role,
                    "gates_acceptance": gates_acceptance,
                    "baseline": base_value,
                    "candidate": candidate_value,
                    "delta": None,
                    "direction": "not_comparable",
                }
            )
            continue
        delta = candidate_value - base_value
        comparisons.append(
            {
                "metric": name,
                "role": role,
                "gates_acceptance": gates_acceptance,
                "baseline": base_value,
                "candidate": candidate_value,
                "delta": delta,
                "direction": metric_direction(name, delta),
            }
        )
    improved = sum(1 for item in comparisons if item["direction"] == "improved")
    regressed = sum(1 for item in comparisons if item["direction"] == "regressed")
    unchanged = sum(1 for item in comparisons if item["direction"] == "unchanged")
    gateable_improved = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "improved"
    )
    gateable_regressed = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "regressed"
    )
    gateable_unchanged = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "unchanged"
    )
    informational_changed = sum(
        1
        for item in comparisons
        if not item["gates_acceptance"] and item["direction"] == "changed"
    )
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "candidate_status": candidate_evaluation.get("status"),
        "comparisons": comparisons,
        "summary": {
            "metric_count": len(comparisons),
            "improved_metric_count": improved,
            "regressed_metric_count": regressed,
            "unchanged_metric_count": unchanged,
            "gateable_metric_count": sum(
                1 for item in comparisons if item["gates_acceptance"]
            ),
            "gateable_improved_metric_count": gateable_improved,
            "gateable_regressed_metric_count": gateable_regressed,
            "gateable_unchanged_metric_count": gateable_unchanged,
            "informational_metric_count": sum(
                1 for item in comparisons if not item["gates_acceptance"]
            ),
            "informational_changed_metric_count": informational_changed,
            "not_comparable_metric_count": sum(
                1 for item in comparisons if item["direction"] == "not_comparable"
            ),
        },
    }


def build_candidate_final_decision(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    schema_decision: dict[str, Any],
    patch: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    metric_delta: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    delta_summary = mapping(metric_delta.get("summary"))
    patch_summary = mapping(patch.get("summary"))
    candidate_status = candidate_evaluation.get("status")
    thresholds = final_decision_thresholds(candidate_evaluation, metric_delta)
    regressed_metric_count = summary_metric_count(
        delta_summary,
        "gateable_regressed_metric_count",
        "regressed_metric_count",
    )
    improved_metric_count = summary_metric_count(
        delta_summary,
        "gateable_improved_metric_count",
        "improved_metric_count",
    )
    stability_summary = mapping(candidate_evaluation.get("stability_summary"))
    flaky_metric_count = int_value(stability_summary.get("flaky_metric_count"))
    if schema_decision.get("decision") != "accepted":
        decision = "rejected"
        reason = "schema_decision_rejected"
    elif proposal.get("status") == "no_op" or patch.get("status") == "no_op":
        decision = "no_op"
        reason = "proposal_has_no_applicable_actions"
    elif int_value(patch_summary.get("applied_action_count")) == 0:
        decision = "rejected"
        reason = "no_safe_actions_applied"
    elif candidate_status in {"error", "failed"}:
        decision = "rejected"
        reason = "candidate_validation_failed"
    elif candidate_status == "not_run":
        decision = "rejected"
        reason = "candidate_validation_not_run"
    elif regressed_metric_count > thresholds["max_regressed_metric_count"]:
        decision = "rejected"
        reason = "candidate_metric_regression"
    elif improved_metric_count < thresholds["min_improved_metric_count"]:
        decision = "rejected"
        reason = "candidate_metric_improvement_below_threshold"
    elif flaky_metric_count > thresholds["max_flaky_metric_count"]:
        decision = "rejected"
        reason = "candidate_stability_below_threshold"
    elif candidate_status in set(thresholds["accepted_candidate_statuses"]):
        decision = "accepted_for_review"
        reason = "candidate_validation_passed_without_regressions"
    else:
        decision = "rejected"
        reason = "candidate_validation_status_not_accepted"
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": patch.get("candidate_id"),
        "decision": decision,
        "reason": reason,
        "application_status": "not_applied",
        "safety": {
            "mainline_modified": False,
            "sandbox_dir": patch.get("sandbox_dir"),
        },
        "summary": {
            "schema_decision": schema_decision.get("decision"),
            "patch_status": patch.get("status"),
            "candidate_status": candidate_status,
            "applied_action_count": int_value(
                patch_summary.get("applied_action_count")
            ),
            "improved_metric_count": int_value(
                delta_summary.get("improved_metric_count")
            ),
            "regressed_metric_count": int_value(
                delta_summary.get("regressed_metric_count")
            ),
            "gateable_improved_metric_count": improved_metric_count,
            "gateable_regressed_metric_count": regressed_metric_count,
            "informational_changed_metric_count": int_value(
                delta_summary.get("informational_changed_metric_count")
            ),
            "flaky_metric_count": flaky_metric_count,
            "acceptance_thresholds": thresholds,
        },
    }


def baseline_metric_snapshot(task: dict[str, Any]) -> dict[str, float | int]:
    summary = mapping(task.get("summary"))
    campaign = mapping(summary.get("campaign"))
    harness = mapping(summary.get("harness"))
    campaign_rollup = mapping(summary.get("campaign_rollup"))
    trace_quality = mapping(task.get("trace_quality"))
    metrics: dict[str, float | int] = {}
    metric_sources = (
        harness,
        campaign,
        campaign_rollup,
        trace_quality,
        {"llm_sample_count": summary.get("llm_sample_count")},
    )
    for source in metric_sources:
        for key in (
            "record_count",
            "failed_record_count",
            "hanging_span_count",
            "orphan_span_count",
            "uncovered_line_count",
            "covered_line_count",
            "coverage_percent",
            "case_count",
            "directive_count",
            "round_count",
            "llm_sample_count",
        ):
            value = number_value(source.get(key))
            if value is not None:
                metrics[key] = value
    return metrics


def numeric_metrics(values: dict[str, Any]) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for key, value in values.items():
        number = number_value(value)
        if number is not None:
            metrics[str(key)] = number
    return metrics


def metric_direction(name: str, delta: float | int) -> str:
    if delta == 0:
        return "unchanged"
    if name in LOWER_IS_BETTER_METRICS:
        return "improved" if delta < 0 else "regressed"
    if name in HIGHER_IS_BETTER_METRICS:
        return "improved" if delta > 0 else "regressed"
    return "changed"


def metric_gates_acceptance(name: str) -> bool:
    return name in LOWER_IS_BETTER_METRICS or name in HIGHER_IS_BETTER_METRICS


def metric_role(name: str) -> str:
    if metric_gates_acceptance(name):
        return "quality_gate"
    if name in INFORMATIONAL_METRICS:
        return "informational"
    return "informational"


def summary_metric_count(
    summary: dict[str, Any],
    preferred_key: str,
    fallback_key: str,
) -> int:
    if preferred_key in summary:
        return int_value(summary.get(preferred_key))
    return int_value(summary.get(fallback_key))


def is_string_or_string_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def is_address_or_address_list(value: Any) -> bool:
    if isinstance(value, list):
        return all(is_address_value(item) for item in value)
    return is_address_value(value)


def is_address_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        try:
            return int(value, 0) >= 0
        except ValueError:
            return False
    return False


def non_negative_int(value: Any) -> bool:
    number = number_value(value)
    return (
        number is not None
        and int(number) == number
        and number >= 0
        and not isinstance(value, bool)
    )


def positive_number(value: Any) -> bool:
    number = number_value(value)
    return number is not None and number > 0


def number_value(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def int_value(value: Any) -> int:
    number = number_value(value)
    if number is None:
        return 0
    return int(number)


def candidate_id_for(task: dict[str, Any], proposal: dict[str, Any]) -> str:
    proposal_id = str(proposal.get("proposal_id") or "proposal")
    run_id = str(task.get("run_id") or "run")
    return f"{safe_slug(run_id)}_{safe_slug(proposal_id)}"


def safe_slug(value: str) -> str:
    chars = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in {"-", "_", "."}):
            chars.append(char)
        else:
            chars.append("_")
    slug = "".join(chars).strip("._")
    return slug or "item"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "BUILTIN_DSL_PAYLOAD_ACTION_TYPES",
    "CANDIDATE_EVALUATION_STATUS_VALUES",
    "DEFAULT_ACCEPTED_CANDIDATE_STATUSES",
    "HIGHER_IS_BETTER_METRICS",
    "INFORMATIONAL_METRICS",
    "LOWER_IS_BETTER_METRICS",
    "PROPOSAL_STATUS_VALUES",
    "SCOREBOARD_CHECK_MODES",
    "action_payload_errors",
    "baseline_metric_snapshot",
    "build_candidate_final_decision",
    "build_candidate_metric_delta",
    "builtin_action_payload_validators",
    "builtin_safe_action_dsl_schema",
    "candidate_evaluation_error",
    "candidate_id_for",
    "collect_record_evidence",
    "coverage_feedback_tuning_payload_errors",
    "evidence_index_for",
    "evidence_ref_error",
    "final_decision_thresholds",
    "int_value",
    "is_address_or_address_list",
    "is_address_value",
    "is_string_or_string_list",
    "metric_direction",
    "metric_gates_acceptance",
    "metric_role",
    "mmio_readback_payload_errors",
    "non_negative_int",
    "normalize_candidate_evaluation",
    "number_value",
    "numeric_metrics",
    "optimizer_proposal_schema_hint",
    "positive_number",
    "replay_probe_payload_errors",
    "safe_action_dsl_schema",
    "safe_slug",
    "scoreboard_check_payload_errors",
    "summary_metric_count",
    "validate_harness_optimization_proposal",
]
