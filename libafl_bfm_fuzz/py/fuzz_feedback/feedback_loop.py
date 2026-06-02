from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


MUTATION_FEEDBACK_SCHEMA_VERSION = 1
MUTATION_FEEDBACK_LAYER = "mutation_feedback"
GAP_FEEDBACK_SCHEMA_VERSION = 1
GAP_FEEDBACK_LAYER = "per_gap_feedback"

MIN_DIRECTION_WEIGHT = 0.1
MAX_DIRECTION_WEIGHT = 4.0
STALE_SUPPRESS_THRESHOLD = 3
GAP_STALE_ESCALATE_THRESHOLD = 2
LOW_PRIORITY_GAP_KINDS = {"toggle", "user"}
COMPLEX_GAP_KINDS = {"branch", "expression", "fsm"}


def build_gap_feedback(
    previous_summary: dict[str, Any] | None,
    current_summary: dict[str, Any],
    directives: dict[str, Any] | list[dict[str, Any]],
    previous_gap_feedback: dict[str, Any] | None = None,
    mutation_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate medium-frequency per-gap progress across mutation rounds.

    Layer 2 tracks whether each RTL gap is new, still open, improved, resolved,
    stale, or regressed. It consumes structured gap ids and evidence point ids,
    but deliberately avoids LLM calls and expensive RTL analysis.
    """

    has_previous_summary = bool(previous_summary)
    previous_summary = previous_summary or {}
    previous_gap_feedback = previous_gap_feedback or {}
    mutation_feedback = mutation_feedback or {}
    target = str(current_summary.get("target") or previous_summary.get("target") or "")

    previous_gaps = gap_index(previous_summary)
    current_gaps = gap_index(current_summary)
    previous_states = previous_gap_states(previous_gap_feedback)
    previous_uncovered = exported_uncovered_point_ids(previous_summary) or uncovered_point_ids(
        previous_summary
    )
    current_uncovered = exported_uncovered_point_ids(current_summary) or uncovered_point_ids(
        current_summary
    )
    attempts_by_gap = directive_gap_attempts(directives)
    direction_feedback = mutation_direction_index(mutation_feedback)

    gap_ids = sorted(set(previous_gaps) | set(current_gaps) | set(previous_states))
    gaps: dict[str, Any] = {}
    counters = {
        "new": 0,
        "open": 0,
        "improved": 0,
        "resolved": 0,
        "stale": 0,
        "regressed": 0,
        "deprioritized": 0,
    }
    next_action_counts: dict[str, int] = {}

    for gap_id in gap_ids:
        previous_gap = previous_gaps.get(gap_id)
        current_gap = current_gaps.get(gap_id)
        previous_state = previous_states.get(gap_id, {})
        attempted_directives = sorted(set(attempts_by_gap.get(gap_id, [])))
        previous_attempted = list(previous_state.get("attempted_directives", []))
        all_attempted = sorted(set(str(item) for item in previous_attempted + attempted_directives))

        previous_count = gap_uncovered_point_count(previous_gap, previous_uncovered)
        if previous_gap is None:
            previous_count = optional_int(previous_state.get("current_point_count"))
        current_count = gap_uncovered_point_count(current_gap, current_uncovered)

        status = classify_gap_status(
            has_previous_summary=has_previous_summary,
            previous_gap=previous_gap,
            current_gap=current_gap,
            previous_state=previous_state,
            previous_count=previous_count,
            current_count=current_count,
            attempted=bool(attempted_directives),
        )
        stale_count = next_gap_stale_count(
            previous_state,
            status=status,
            attempted=bool(attempted_directives),
        )
        attempt_count = int(previous_state.get("attempt_count", 0)) + len(attempted_directives)
        success_count = int(previous_state.get("success_count", 0))
        if status in {"improved", "resolved"}:
            success_count += 1

        representative = current_gap or previous_gap or previous_state
        primary_kind = str(representative.get("primary_kind", "unknown"))
        next_action = select_gap_next_action(
            status=status,
            primary_kind=primary_kind,
            stale_count=stale_count,
            attempted_directives=attempted_directives,
            direction_feedback=direction_feedback,
        )
        if next_action == "deprioritize":
            counters["deprioritized"] += 1
        counters[status] = counters.get(status, 0) + 1
        next_action_counts[next_action] = next_action_counts.get(next_action, 0) + 1

        gap_record = compact_gap_feedback_record(
            gap_id=gap_id,
            representative=representative,
            status=status,
            next_action=next_action,
            previous_count=previous_count,
            current_count=current_count,
            attempt_count=attempt_count,
            stale_count=stale_count,
            success_count=success_count,
            attempted_directives=all_attempted,
            current_attempted_directives=attempted_directives,
            direction_feedback=direction_feedback,
        )
        gaps[gap_id] = gap_record

    return {
        "schema_version": GAP_FEEDBACK_SCHEMA_VERSION,
        "layer": GAP_FEEDBACK_LAYER,
        "target": target,
        "total_gaps": len(gaps),
        "status_counts": {key: value for key, value in sorted(counters.items()) if value},
        "next_action_counts": dict(sorted(next_action_counts.items())),
        "gaps": gaps,
    }


def build_mutation_feedback(
    previous_summary: dict[str, Any] | None,
    current_summary: dict[str, Any],
    directives: dict[str, Any] | list[dict[str, Any]],
    previous_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the high-frequency effect of mutation directions.

    Layer 3 intentionally avoids LLM and heavy RTL analysis. It consumes aggregate
    deltas that are already cheap to compute: directive origin counts, newly hit
    structural coverage points, and newly hit functional bins.
    """

    has_previous_summary = bool(previous_summary)
    previous_summary = previous_summary or {}
    previous_feedback = previous_feedback or {}
    target = str(current_summary.get("target") or previous_summary.get("target") or "")
    directive_list = mutation_directives(directives)

    previous_origin_counts = stimulus_origin_counts(previous_summary)
    current_origin_counts = stimulus_origin_counts(current_summary)
    previous_replay_origin_counts = functional_origin_counts(previous_summary)
    current_replay_origin_counts = functional_origin_counts(current_summary)

    previous_uncovered_points = uncovered_point_ids(previous_summary)
    current_uncovered_points = uncovered_point_ids(current_summary)
    resolved_point_ids = (
        sorted(previous_uncovered_points - current_uncovered_points)
        if has_previous_summary
        else []
    )

    previous_gap_ids = open_gap_ids(previous_summary)
    current_gap_ids = open_gap_ids(current_summary)
    resolved_gap_ids = sorted(previous_gap_ids - current_gap_ids) if has_previous_summary else []

    previous_functional_hits = functional_hit_ids(previous_summary)
    current_functional_hits = functional_hit_ids(current_summary)
    new_functional_bins = (
        sorted(current_functional_hits - previous_functional_hits)
        if has_previous_summary
        else []
    )

    structural_coverage_delta = (
        coverage_delta(previous_summary, current_summary) if has_previous_summary else 0.0
    )
    uncovered_line_delta = (
        int(previous_summary.get("uncovered_line_count", 0))
        - int(current_summary.get("uncovered_line_count", 0))
        if has_previous_summary
        else 0
    )

    direction_names = [
        direction_name(directive, idx)
        for idx, directive in enumerate(directive_list)
        if isinstance(directive, dict)
    ]
    generated_deltas = {
        name: max(
            0,
            current_origin_counts.get(name, 0) - previous_origin_counts.get(name, 0),
        )
        for name in direction_names
    }
    active_names = [name for name, count in generated_deltas.items() if count > 0]
    total_generated_delta = sum(generated_deltas.values())
    attribution = "direct" if len(active_names) <= 1 else "mixed_proportional"

    directions: dict[str, Any] = {}
    for idx, directive in enumerate(directive_list):
        if not isinstance(directive, dict):
            continue
        name = direction_name(directive, idx)
        generated_delta = generated_deltas.get(name, 0)
        replay_delta = max(
            0,
            current_replay_origin_counts.get(name, 0)
            - previous_replay_origin_counts.get(name, 0),
        )
        share = direction_share(name, generated_delta, active_names, total_generated_delta)
        allocated_points = len(resolved_point_ids) * share
        allocated_gaps = len(resolved_gap_ids) * share
        allocated_functional_bins = len(new_functional_bins) * share
        replay_drop = max(0, generated_delta - replay_delta)

        previous_direction = previous_direction_feedback(previous_feedback, name)
        stale_count = next_stale_count(
            previous_direction,
            generated_delta=generated_delta,
            allocated_points=allocated_points,
            allocated_gaps=allocated_gaps,
            allocated_functional_bins=allocated_functional_bins,
            structural_coverage_delta=structural_coverage_delta,
        )
        score = mutation_direction_score(
            generated_delta=generated_delta,
            allocated_points=allocated_points,
            allocated_gaps=allocated_gaps,
            allocated_functional_bins=allocated_functional_bins,
            structural_coverage_delta=structural_coverage_delta * share,
            replay_drop=replay_drop,
        )
        decision = mutation_direction_decision(
            generated_delta=generated_delta,
            score=score,
            stale_count=stale_count,
            replay_drop=replay_drop,
        )
        current_weight = direction_weight(directive, previous_direction)
        updated_weight = updated_direction_weight(current_weight, decision)
        success_count = int(previous_direction.get("success_count", 0))
        if has_positive_gain(
            allocated_points,
            allocated_gaps,
            allocated_functional_bins,
            structural_coverage_delta,
        ):
            success_count += 1

        directions[name] = {
            "name": name,
            "target": directive.get("target", target),
            "gap_ids": list(directive.get("gap_ids", []))
            if isinstance(directive.get("gap_ids", []), list)
            else [],
            "generated_cases": current_origin_counts.get(name, 0),
            "new_generated_cases": generated_delta,
            "replayed_cases": current_replay_origin_counts.get(name, 0),
            "new_replayed_cases": replay_delta,
            "replay_drop": replay_drop,
            "attribution": attribution,
            "attribution_share": round(share, 6),
            "structural_resolved_points": round(allocated_points, 6),
            "resolved_gap_count": round(allocated_gaps, 6),
            "functional_new_bins": round(allocated_functional_bins, 6),
            "score": score,
            "decision": decision,
            "previous_weight": round(current_weight, 6),
            "updated_weight": updated_weight,
            "stale_count": stale_count,
            "success_count": success_count,
        }

    return {
        "schema_version": MUTATION_FEEDBACK_SCHEMA_VERSION,
        "layer": MUTATION_FEEDBACK_LAYER,
        "target": target,
        "attribution": attribution,
        "aggregate_delta": {
            "structural_resolved_point_count": len(resolved_point_ids),
            "resolved_gap_count": len(resolved_gap_ids),
            "functional_new_bin_count": len(new_functional_bins),
            "structural_coverage_delta": round(structural_coverage_delta, 6),
            "uncovered_line_delta": uncovered_line_delta,
            "total_new_generated_cases": total_generated_delta,
            "resolved_point_ids": resolved_point_ids[:64],
            "resolved_gap_ids": resolved_gap_ids[:64],
            "new_functional_bins": new_functional_bins[:64],
        },
        "directions": directions,
    }


def update_mutation_directions(
    directives: dict[str, Any] | list[dict[str, Any]],
    mutation_feedback: dict[str, Any],
) -> dict[str, Any]:
    """Return directives annotated and ordered by Layer 3 feedback."""

    value = deepcopy(directives)
    if isinstance(value, dict):
        result = dict(value)
        directive_list = mutation_directives(value)
    else:
        result = {"source": "mutation_feedback", "directives": []}
        directive_list = mutation_directives(value)

    feedback_by_name = mutation_feedback.get("directions", {})
    updated = []
    for idx, directive in enumerate(directive_list):
        if not isinstance(directive, dict):
            continue
        item = dict(directive)
        name = direction_name(item, idx)
        direction_feedback = feedback_by_name.get(name)
        if isinstance(direction_feedback, dict):
            item["weight"] = direction_feedback.get("updated_weight", item.get("weight", 1))
            item["feedback_decision"] = direction_feedback.get("decision")
            item["feedback_score"] = direction_feedback.get("score")
            if direction_feedback.get("decision") == "suppress_temporarily":
                item["enabled"] = False
        updated.append(item)

    updated.sort(key=directive_sort_key)
    result["directives"] = updated
    result["mutation_feedback"] = {
        "schema_version": mutation_feedback.get("schema_version", MUTATION_FEEDBACK_SCHEMA_VERSION),
        "layer": mutation_feedback.get("layer", MUTATION_FEEDBACK_LAYER),
        "aggregate_delta": mutation_feedback.get("aggregate_delta", {}),
    }
    return result


def gap_index(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(gap["id"]): gap
        for gap in rtl_gaps(summary)
        if isinstance(gap, dict) and gap.get("id") is not None
    }


def previous_gap_states(previous_gap_feedback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gaps = previous_gap_feedback.get("gaps", {})
    if isinstance(gaps, dict):
        return {str(gap_id): state for gap_id, state in gaps.items() if isinstance(state, dict)}
    return {}


def directive_gap_attempts(
    directives: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, list[str]]:
    attempts: dict[str, list[str]] = {}
    for idx, directive in enumerate(mutation_directives(directives)):
        name = direction_name(directive, idx)
        raw_gap_ids = directive.get("gap_ids", [])
        if not isinstance(raw_gap_ids, list):
            continue
        for gap_id in raw_gap_ids:
            attempts.setdefault(str(gap_id), []).append(name)
    return attempts


def mutation_direction_index(mutation_feedback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    directions = mutation_feedback.get("directions", {})
    if isinstance(directions, dict):
        return {
            str(name): direction
            for name, direction in directions.items()
            if isinstance(direction, dict)
        }
    return {}


def gap_uncovered_point_count(
    gap: dict[str, Any] | None,
    uncovered_ids: set[str],
) -> int | None:
    if gap is None:
        return None
    point_ids = gap_point_ids(gap)
    if point_ids:
        if uncovered_ids:
            return sum(1 for point_id in point_ids if point_id in uncovered_ids)
        return len(point_ids)
    count = optional_int(gap.get("point_count"))
    return count


def gap_point_ids(gap: dict[str, Any]) -> list[str]:
    evidence = gap.get("evidence", {})
    if not isinstance(evidence, dict):
        return []
    point_ids = evidence.get("point_ids", [])
    if not isinstance(point_ids, list):
        return []
    return [str(point_id) for point_id in point_ids]


def optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_gap_status(
    *,
    has_previous_summary: bool,
    previous_gap: dict[str, Any] | None,
    current_gap: dict[str, Any] | None,
    previous_state: dict[str, Any],
    previous_count: int | None,
    current_count: int | None,
    attempted: bool,
) -> str:
    previous_status = str(previous_state.get("status", ""))
    if current_gap is None:
        if previous_gap is not None or previous_state:
            return "resolved"
        return "open"
    if previous_status == "resolved" and previous_gap is None and current_gap is not None:
        return "regressed"
    if not has_previous_summary and not previous_state:
        return "new"
    if previous_gap is None and not previous_state:
        return "new"
    if (
        previous_count is not None
        and current_count is not None
        and current_count < previous_count
    ):
        return "improved"
    if attempted:
        return "stale"
    return "open"


def next_gap_stale_count(
    previous_state: dict[str, Any],
    *,
    status: str,
    attempted: bool,
) -> int:
    previous_stale = int(previous_state.get("stale_count", 0))
    if status in {"improved", "resolved", "new", "regressed"}:
        return 0
    if status == "stale" and attempted:
        return previous_stale + 1
    return previous_stale


def select_gap_next_action(
    *,
    status: str,
    primary_kind: str,
    stale_count: int,
    attempted_directives: list[str],
    direction_feedback: dict[str, dict[str, Any]],
) -> str:
    if status == "resolved":
        return "done"
    if status == "improved":
        return "continue"
    if status == "regressed":
        return "retry"
    if status == "new":
        return "plan"

    attempted_decisions = {
        str(direction_feedback.get(name, {}).get("decision", ""))
        for name in attempted_directives
    }
    if "suppress_temporarily" in attempted_decisions:
        return "try_alternative"
    if stale_count >= GAP_STALE_ESCALATE_THRESHOLD:
        if primary_kind in COMPLEX_GAP_KINDS:
            return "escalate_to_llm"
        if primary_kind in LOW_PRIORITY_GAP_KINDS:
            return "deprioritize"
        return "try_alternative"
    if attempted_directives:
        return "continue"
    return "plan"


def compact_gap_feedback_record(
    *,
    gap_id: str,
    representative: dict[str, Any],
    status: str,
    next_action: str,
    previous_count: int | None,
    current_count: int | None,
    attempt_count: int,
    stale_count: int,
    success_count: int,
    attempted_directives: list[str],
    current_attempted_directives: list[str],
    direction_feedback: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    direction_decisions = {
        name: direction_feedback.get(name, {}).get("decision")
        for name in current_attempted_directives
        if name in direction_feedback
    }
    return {
        "id": gap_id,
        "status": status,
        "next_action": next_action,
        "primary_kind": representative.get("primary_kind"),
        "priority": representative.get("priority"),
        "file": representative.get("file"),
        "line": representative.get("line"),
        "module": representative.get("module"),
        "code": representative.get("code", ""),
        "previous_point_count": previous_count,
        "current_point_count": current_count,
        "attempt_count": attempt_count,
        "stale_count": stale_count,
        "success_count": success_count,
        "attempted_directives": attempted_directives,
        "current_attempted_directives": current_attempted_directives,
        "direction_decisions": direction_decisions,
        "evidence": representative.get("evidence", {}),
    }


def mutation_directives(value: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    items = value.get("directives", [])
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def direction_name(directive: dict[str, Any], index: int) -> str:
    name = directive.get("name")
    if isinstance(name, str) and name:
        return name
    return f"directive_{index}"


def stimulus_origin_counts(summary: dict[str, Any]) -> dict[str, int]:
    return int_counter(summary.get("stimulus_summary", {}).get("origin_counts", {}))


def functional_origin_counts(summary: dict[str, Any]) -> dict[str, int]:
    return int_counter(summary.get("uvm_functional_coverage", {}).get("origin_counts", {}))


def int_counter(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, count in value.items():
        try:
            result[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return result


def exported_uncovered_point_ids(summary: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    structure = summary.get("rtl_structure_coverage", {})
    exports = [
        structure,
        structure.get("coverage_export", {}) if isinstance(structure, dict) else {},
    ]
    for export in exports:
        if not isinstance(export, dict):
            continue
        for point in export.get("uncovered_points", []):
            if isinstance(point, dict) and point.get("id") is not None:
                ids.add(str(point["id"]))
    return ids


def uncovered_point_ids(summary: dict[str, Any]) -> set[str]:
    ids = set(exported_uncovered_point_ids(summary))

    for gap in rtl_gaps(summary):
        evidence = gap.get("evidence", {})
        if not isinstance(evidence, dict):
            continue
        for point_id in evidence.get("point_ids", []):
            ids.add(str(point_id))
    return ids


def open_gap_ids(summary: dict[str, Any]) -> set[str]:
    return {str(gap["id"]) for gap in rtl_gaps(summary) if gap.get("id") is not None}


def rtl_gaps(summary: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = summary.get("rtl_gap_summary", {}).get("top_gaps", [])
    if isinstance(gaps, list):
        return [gap for gap in gaps if isinstance(gap, dict)]
    return []


def functional_hit_ids(summary: dict[str, Any]) -> set[str]:
    coverage = summary.get("uvm_functional_coverage", {})
    if not isinstance(coverage, dict):
        return set()
    hit_ids: set[str] = set()
    for section in ("bins", "crosses"):
        values = coverage.get(section, {})
        if not isinstance(values, dict):
            continue
        for name, counters in values.items():
            if not isinstance(counters, dict):
                continue
            for value, count in counters.items():
                try:
                    numeric_count = int(count)
                except (TypeError, ValueError):
                    continue
                if numeric_count > 0:
                    hit_ids.add(f"{section}:{name}:{value}")
    return hit_ids


def coverage_delta(previous_summary: dict[str, Any], current_summary: dict[str, Any]) -> float:
    return coverage_value(current_summary) - coverage_value(previous_summary)


def coverage_value(summary: dict[str, Any]) -> float:
    structure = summary.get("rtl_structure_coverage", {})
    if isinstance(structure, dict):
        totals = structure.get("totals", {})
        if isinstance(totals, dict) and totals.get("coverage") is not None:
            return float(totals["coverage"])
        export = structure.get("coverage_export", {})
        if isinstance(export, dict):
            totals = export.get("totals", {})
            if isinstance(totals, dict) and totals.get("coverage") is not None:
                return float(totals["coverage"])
    return 0.0


def direction_share(
    name: str,
    generated_delta: int,
    active_names: list[str],
    total_generated_delta: int,
) -> float:
    if generated_delta <= 0:
        return 0.0
    if len(active_names) <= 1:
        return 1.0
    if total_generated_delta <= 0:
        return 0.0
    return generated_delta / total_generated_delta


def previous_direction_feedback(previous_feedback: dict[str, Any], name: str) -> dict[str, Any]:
    directions = previous_feedback.get("directions", {})
    if isinstance(directions, dict) and isinstance(directions.get(name), dict):
        return directions[name]
    return {}


def next_stale_count(
    previous_direction: dict[str, Any],
    *,
    generated_delta: int,
    allocated_points: float,
    allocated_gaps: float,
    allocated_functional_bins: float,
    structural_coverage_delta: float,
) -> int:
    previous_stale = int(previous_direction.get("stale_count", 0))
    if generated_delta <= 0:
        return previous_stale
    if has_positive_gain(
        allocated_points,
        allocated_gaps,
        allocated_functional_bins,
        structural_coverage_delta,
    ):
        return 0
    return previous_stale + 1


def has_positive_gain(
    allocated_points: float,
    allocated_gaps: float,
    allocated_functional_bins: float,
    structural_coverage_delta: float,
) -> bool:
    return (
        allocated_points > 0
        or allocated_gaps > 0
        or allocated_functional_bins > 0
        or structural_coverage_delta > 0
    )


def mutation_direction_score(
    *,
    generated_delta: int,
    allocated_points: float,
    allocated_gaps: float,
    allocated_functional_bins: float,
    structural_coverage_delta: float,
    replay_drop: int,
) -> float:
    if generated_delta <= 0:
        return 0.0
    gain = (
        allocated_points * 2.0
        + allocated_gaps * 3.0
        + allocated_functional_bins
        + max(0.0, structural_coverage_delta) * 10.0
    )
    efficiency = gain / math.sqrt(max(1, generated_delta))
    score = efficiency - replay_drop * 0.25
    return round(score, 6)


def mutation_direction_decision(
    *,
    generated_delta: int,
    score: float,
    stale_count: int,
    replay_drop: int,
) -> str:
    if generated_delta <= 0:
        return "inactive"
    if replay_drop >= max(3, generated_delta // 2):
        return "decrease_weight"
    if score > 0:
        return "increase_weight"
    if stale_count >= STALE_SUPPRESS_THRESHOLD:
        return "suppress_temporarily"
    return "decrease_weight"


def direction_weight(directive: dict[str, Any], previous_direction: dict[str, Any]) -> float:
    for value in (previous_direction.get("updated_weight"), directive.get("weight")):
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 1.0


def updated_direction_weight(current_weight: float, decision: str) -> float:
    if decision == "increase_weight":
        return round(min(MAX_DIRECTION_WEIGHT, max(current_weight + 0.25, current_weight * 1.5)), 6)
    if decision == "decrease_weight":
        return round(max(MIN_DIRECTION_WEIGHT, current_weight * 0.7), 6)
    if decision == "suppress_temporarily":
        return MIN_DIRECTION_WEIGHT
    return round(current_weight, 6)


def directive_sort_key(directive: dict[str, Any]) -> tuple[int, float, str]:
    enabled = directive.get("enabled", True) is not False
    try:
        score = float(directive.get("feedback_score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return (0 if enabled else 1, -score, str(directive.get("name", "")))
