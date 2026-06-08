#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
FUZZ_DIR = REPO_ROOT / "libafl_bfm_fuzz"
PY_DIR = FUZZ_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from connector_observe import Connector, ObservationContext, observer_from_env  # noqa: E402
from fuzz_feedback.advisors import propose_directives_from_plan  # noqa: E402
from fuzz_feedback.feedback_loop import build_gap_feedback, build_mutation_feedback  # noqa: E402
from fuzz_feedback.mutation_planner import plan_mutations_from_rtl_gaps  # noqa: E402
from fuzz_pipeline.coverage_feedback import (  # noqa: E402
    directives_metrics,
    gap_feedback_metrics,
    layer1_plan_metrics,
    mutation_feedback_metrics,
    write_json as write_pipeline_json,
)
from fuzz_pipeline.topology import FULL_FUZZ_TOPOLOGY  # noqa: E402


DEFAULT_ROUNDS = ("baseline", "heuristic", "llm")


def main() -> int:
    args = parse_args()
    round_names = args.round or list(DEFAULT_ROUNDS)
    round_paths = resolve_round_paths(args.run_dir, round_names)
    runs = [load_run(args.target, name, path) for name, path in zip(round_names, round_paths)]
    if len(runs) < 2:
        raise SystemExit("provide at least two rounds to evaluate feedback transitions")

    target = args.target or str(runs[0]["summary"].get("target") or "target")
    output_dir = args.out_dir or args.run_dir / "three_layer_eval"
    output_dir.mkdir(parents=True, exist_ok=True)

    observer = observer_from_env(
        args.observation_out,
        monitoring_path=args.monitoring_out,
    )
    context = ObservationContext.from_env(observer=observer)
    if args.observation_run_id:
        context = ObservationContext(
            run_id=args.observation_run_id,
            observer=context.observer,
            strict=context.strict,
        )

    try:
        if args.topology_out:
            write_pipeline_json(args.topology_out, FULL_FUZZ_TOPOLOGY.to_json())
        evaluation = build_evaluation(target, runs, output_dir, context)
    finally:
        observer.close()
    json_out = args.json_out or output_dir / f"{target}_three_layer_feedback_eval.json"
    markdown_out = args.markdown_out or output_dir / f"{target}_three_layer_feedback_eval.md"
    write_json(json_out, evaluation)
    markdown = render_markdown(evaluation)
    markdown_out.write_text(markdown)

    print(markdown)
    print(f"Wrote {json_out}")
    print(f"Wrote {markdown_out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate multi-round Layer 1/2/3 coverage feedback evaluation reports."
        )
    )
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Directory containing per-round subdirectories such as baseline/heuristic/llm.",
    )
    parser.add_argument(
        "--round",
        action="append",
        help="Round subdirectory name. Repeat to control order. Default: baseline, heuristic, llm.",
    )
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--observation-out", type=Path)
    parser.add_argument("--monitoring-out", type=Path)
    parser.add_argument("--topology-out", type=Path)
    parser.add_argument("--observation-run-id")
    return parser.parse_args()


def resolve_round_paths(run_dir: Path, round_names: list[str]) -> list[Path]:
    paths = [run_dir / name for name in round_names]
    missing = [path for path in paths if not path.exists()]
    if missing:
        raise SystemExit("missing round dir(s): " + ", ".join(str(path) for path in missing))
    return paths


def load_run(target: str, name: str, run_dir: Path) -> dict[str, Any]:
    summary_path = run_dir / f"{target}_coverage_summary.json"
    directives_path = run_dir / f"{target}_mutation_directives.json"
    round_applied_directives_path = run_dir / f"{target}_{name}_directives.json"
    functional_path = run_dir / f"{target}_uvm_functional_coverage.json"
    summary = load_json(summary_path)
    directives = load_json(directives_path)
    round_applied_directives = (
        load_json(round_applied_directives_path)
        if round_applied_directives_path.exists()
        else None
    )
    functional = load_json(functional_path) if functional_path.exists() else {}
    return {
        "name": name,
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "directives_path": str(directives_path),
        "round_applied_directives_path": (
            str(round_applied_directives_path) if round_applied_directives is not None else None
        ),
        "functional_path": str(functional_path) if functional_path.exists() else None,
        "summary": summary,
        "directives": directives,
        "round_applied_directives": round_applied_directives,
        "functional": functional,
    }


def build_evaluation(
    target: str,
    runs: list[dict[str, Any]],
    output_dir: Path,
    context: ObservationContext | None = None,
) -> dict[str, Any]:
    context = context or ObservationContext()
    round_rows = [summarize_round(run) for run in runs]
    transitions: list[dict[str, Any]] = []
    previous_gap_feedback: dict[str, Any] | None = None
    previous_mutation_feedback: dict[str, Any] | None = None

    for index in range(len(runs) - 1):
        previous_run = runs[index]
        current_run = runs[index + 1]
        previous_summary = previous_run["summary"]
        current_summary = current_run["summary"]
        applied_directives = transition_applied_directives(previous_run, current_run)

        mutation_feedback = connector(
            "summary_to_mutation_feedback",
            "coverage_summary",
            "mutation_feedback",
            context,
        ).run(
            build_mutation_feedback,
            previous_summary,
            current_summary,
            applied_directives,
            previous_feedback=previous_mutation_feedback,
            metrics=mutation_feedback_metrics,
            metadata={
                "target": target,
                "transition": f"{previous_run['name']}->{current_run['name']}",
            },
        )
        gap_feedback = connector(
            "layer3_feedback_to_layer2_feedback",
            "mutation_feedback",
            "gap_feedback",
            context,
        ).run(
            build_gap_feedback,
            previous_summary,
            current_summary,
            applied_directives,
            previous_gap_feedback=previous_gap_feedback,
            mutation_feedback=mutation_feedback,
            metrics=gap_feedback_metrics,
            metadata={
                "target": target,
                "transition": f"{previous_run['name']}->{current_run['name']}",
            },
        )
        structural_plan = connector(
            "layer2_layer3_feedback_to_layer1_plan",
            "gap_feedback",
            "layer1_plan",
            context,
        ).run(
            plan_mutations_from_rtl_gaps,
            current_summary,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
            metrics=layer1_plan_metrics,
            metadata={
                "target": target,
                "transition": f"{previous_run['name']}->{current_run['name']}",
            },
        )
        next_directives = connector(
            "layer1_plan_to_directives",
            "layer1_plan",
            "mutation_directives",
            context,
        ).run(
            propose_directives_from_plan,
            current_summary,
            structural_plan,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
            metrics=directives_metrics,
            metadata={
                "target": target,
                "transition": f"{previous_run['name']}->{current_run['name']}",
            },
        )

        prefix = f"transition_{index:02d}_{previous_run['name']}_to_{current_run['name']}"
        layer2_out = output_dir / f"{prefix}_layer2_gap_feedback.json"
        layer3_out = output_dir / f"{prefix}_layer3_mutation_feedback.json"
        layer1_out = output_dir / f"{prefix}_layer1_plan.json"
        directives_out = output_dir / f"{prefix}_layer1_directives.json"
        write_json(layer2_out, gap_feedback)
        write_json(layer3_out, mutation_feedback)
        write_json(layer1_out, structural_plan)
        write_json(directives_out, next_directives)

        transitions.append(
            summarize_transition(
                index=index,
                previous_run=previous_run,
                current_run=current_run,
                gap_feedback=gap_feedback,
                mutation_feedback=mutation_feedback,
                structural_plan=structural_plan,
                next_directives=next_directives,
                applied_directives=applied_directives,
                artifact_paths={
                    "layer2_gap_feedback": str(layer2_out),
                    "layer3_mutation_feedback": str(layer3_out),
                    "layer1_plan": str(layer1_out),
                    "layer1_directives": str(directives_out),
                },
            )
        )
        previous_gap_feedback = gap_feedback
        previous_mutation_feedback = mutation_feedback

    return {
        "schema_version": 1,
        "target": target,
        "rounds": round_rows,
        "transitions": transitions,
        "summary": summarize_overall(round_rows, transitions),
    }


def connector(name: str, from_layer: str, to_layer: str, context: ObservationContext) -> Connector:
    return Connector.from_context(name, from_layer, to_layer, context)


def summarize_round(run: dict[str, Any]) -> dict[str, Any]:
    summary = run["summary"]
    structure = summary.get("rtl_structure_coverage", {})
    by_kind = structure.get("by_kind", {}) if isinstance(structure, dict) else {}
    functional = run.get("functional") or summary.get("uvm_functional_coverage", {})
    return {
        "name": run["name"],
        "run_dir": run["run_dir"],
        "cases": total_cases(summary, functional),
        "functional_uncovered": functional.get("uncovered", {}) if isinstance(functional, dict) else {},
        "open_gap_count": open_gap_count(summary),
        "uncovered_lines": int(summary.get("uncovered_line_count", 0)),
        "overall": coverage_entry(structure),
        "line": coverage_entry(by_kind.get("line", {})),
        "toggle": coverage_entry(by_kind.get("toggle", {})),
        "branch": coverage_entry(by_kind.get("branch", {})),
        "expression": coverage_entry(by_kind.get("expression", {})),
    }


def summarize_transition(
    *,
    index: int,
    previous_run: dict[str, Any],
    current_run: dict[str, Any],
    gap_feedback: dict[str, Any],
    mutation_feedback: dict[str, Any],
    structural_plan: dict[str, Any],
    next_directives: dict[str, Any],
    applied_directives: dict[str, Any],
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    previous_summary = previous_run["summary"]
    current_summary = current_run["summary"]
    targeted_gap_ids = directive_gap_ids(applied_directives)
    targeted_records = [
        gap
        for gap_id, gap in gap_feedback.get("gaps", {}).items()
        if str(gap_id) in targeted_gap_ids and isinstance(gap, dict)
    ]
    selection = structural_plan.get("gap_selection", {})
    aggregate = mutation_feedback.get("aggregate_delta", {})
    directions = mutation_feedback.get("directions", {})
    return {
        "index": index,
        "from": previous_run["name"],
        "to": current_run["name"],
        "applied_directives": applied_directives_path(previous_run, current_run),
        "coverage_delta": coverage_delta_summary(previous_summary, current_summary),
        "layer2": {
            "targeted_gap_count": len(targeted_gap_ids),
            "resolved_targeted_gap_count": count_status(targeted_records, "resolved"),
            "improved_targeted_gap_count": count_status(targeted_records, "improved"),
            "stale_targeted_gap_count": count_status(targeted_records, "stale"),
            "status_counts": gap_feedback.get("status_counts", {}),
            "next_action_counts": gap_feedback.get("next_action_counts", {}),
            "high_value_actions": high_value_gap_actions(gap_feedback),
        },
        "layer3": {
            "attribution": mutation_feedback.get("attribution", ""),
            "aggregate_delta": aggregate,
            "decision_counts": decision_counts(directions),
            "best_direction": best_direction(directions),
            "suppressed_directions": suppressed_directions(directions),
        },
        "layer1": {
            "source": next_directives.get("source", ""),
            "directive_count": len(mutation_directives(next_directives)),
            "gap_selection_counts": selection_counts(selection),
            "complex_gap_ids": list_values(selection.get("complex_for_llm", [])),
            "skipped_gap_ids": list_values(selection.get("skipped", [])),
            "blocked_gap_ids": list_values(selection.get("blocked", [])),
        },
        "artifacts": artifact_paths,
    }


def transition_applied_directives(
    previous_run: dict[str, Any],
    current_run: dict[str, Any],
) -> dict[str, Any]:
    round_applied = current_run.get("round_applied_directives")
    if isinstance(round_applied, dict):
        return round_applied
    return previous_run["directives"]


def applied_directives_path(previous_run: dict[str, Any], current_run: dict[str, Any]) -> str:
    round_path = current_run.get("round_applied_directives_path")
    if isinstance(round_path, str) and round_path:
        return round_path
    return str(previous_run["directives_path"])


def summarize_overall(
    rounds: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rounds:
        return {}
    first = rounds[0]
    last = rounds[-1]
    return {
        "round_count": len(rounds),
        "transition_count": len(transitions),
        "overall_coverage_delta": coverage_delta_value(first["overall"], last["overall"]),
        "uncovered_line_delta": last["uncovered_lines"] - first["uncovered_lines"],
        "open_gap_delta": last["open_gap_count"] - first["open_gap_count"],
        "total_resolved_targeted_gaps": sum(
            int(item["layer2"]["resolved_targeted_gap_count"]) for item in transitions
        ),
        "total_improved_targeted_gaps": sum(
            int(item["layer2"]["improved_targeted_gap_count"]) for item in transitions
        ),
        "total_suppressed_directions": sum(
            len(item["layer3"]["suppressed_directions"]) for item in transitions
        ),
    }


def coverage_delta_summary(
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
) -> dict[str, Any]:
    previous_structure = previous_summary.get("rtl_structure_coverage", {})
    current_structure = current_summary.get("rtl_structure_coverage", {})
    previous_by_kind = previous_structure.get("by_kind", {}) if isinstance(previous_structure, dict) else {}
    current_by_kind = current_structure.get("by_kind", {}) if isinstance(current_structure, dict) else {}
    return {
        "overall": coverage_delta_value(coverage_entry(previous_structure), coverage_entry(current_structure)),
        "line": coverage_delta_value(coverage_entry(previous_by_kind.get("line", {})), coverage_entry(current_by_kind.get("line", {}))),
        "toggle": coverage_delta_value(coverage_entry(previous_by_kind.get("toggle", {})), coverage_entry(current_by_kind.get("toggle", {}))),
        "branch": coverage_delta_value(coverage_entry(previous_by_kind.get("branch", {})), coverage_entry(current_by_kind.get("branch", {}))),
        "expression": coverage_delta_value(coverage_entry(previous_by_kind.get("expression", {})), coverage_entry(current_by_kind.get("expression", {}))),
        "uncovered_lines": int(current_summary.get("uncovered_line_count", 0))
        - int(previous_summary.get("uncovered_line_count", 0)),
        "open_gaps": open_gap_count(current_summary) - open_gap_count(previous_summary),
    }


def total_cases(summary: dict[str, Any], functional: Any) -> int:
    if isinstance(functional, dict):
        try:
            return int(functional.get("total_cases"))
        except (TypeError, ValueError):
            pass
    try:
        return int(summary.get("stimulus_summary", {}).get("total_cases"))
    except (TypeError, ValueError):
        return 0


def coverage_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    totals = value.get("totals")
    if isinstance(totals, dict):
        return totals
    return value


def coverage_delta_value(previous: dict[str, Any], current: dict[str, Any]) -> float | None:
    try:
        return round(float(current["coverage"]) - float(previous["coverage"]), 6)
    except (KeyError, TypeError, ValueError):
        return None


def open_gap_count(summary: dict[str, Any]) -> int:
    gaps = summary.get("rtl_gap_summary", {}).get("top_gaps", [])
    return len(gaps) if isinstance(gaps, list) else 0


def directive_gap_ids(directives: dict[str, Any]) -> set[str]:
    gap_ids: set[str] = set()
    for directive in mutation_directives(directives):
        raw_gap_ids = directive.get("gap_ids", [])
        if isinstance(raw_gap_ids, list):
            gap_ids.update(str(gap_id) for gap_id in raw_gap_ids)
    return gap_ids


def mutation_directives(value: dict[str, Any]) -> list[dict[str, Any]]:
    items = value.get("directives", []) if isinstance(value, dict) else []
    return [item for item in items if isinstance(item, dict)]


def count_status(records: list[dict[str, Any]], status: str) -> int:
    return sum(1 for record in records if record.get("status") == status)


def decision_counts(directions: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(directions, dict):
        return counts
    for direction in directions.values():
        if not isinstance(direction, dict):
            continue
        decision = str(direction.get("decision", "unknown"))
        counts[decision] = counts.get(decision, 0) + 1
    return dict(sorted(counts.items()))


def best_direction(directions: Any) -> str | None:
    if not isinstance(directions, dict) or not directions:
        return None
    best_name = None
    best_score = None
    for name, direction in directions.items():
        if not isinstance(direction, dict):
            continue
        try:
            score = float(direction.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        if best_score is None or score > best_score:
            best_score = score
            best_name = str(name)
    return best_name


def suppressed_directions(directions: Any) -> list[str]:
    if not isinstance(directions, dict):
        return []
    return sorted(
        str(name)
        for name, direction in directions.items()
        if isinstance(direction, dict) and direction.get("decision") == "suppress_temporarily"
    )


def high_value_gap_actions(gap_feedback: dict[str, Any]) -> list[dict[str, Any]]:
    high_value = {"escalate_to_llm", "try_alternative", "retry", "deprioritize"}
    gaps = gap_feedback.get("gaps", {})
    if not isinstance(gaps, dict):
        return []
    result = []
    for gap in gaps.values():
        if not isinstance(gap, dict) or gap.get("next_action") not in high_value:
            continue
        result.append(
            {
                "id": gap.get("id"),
                "status": gap.get("status"),
                "next_action": gap.get("next_action"),
                "primary_kind": gap.get("primary_kind"),
                "stale_count": gap.get("stale_count", 0),
                "attempt_count": gap.get("attempt_count", 0),
            }
        )
    return sorted(result, key=lambda item: str(item.get("id", "")))[:24]


def selection_counts(selection: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(selection, dict):
        return result
    for key in ("heuristic", "complex_for_llm", "skipped", "deprioritized", "blocked"):
        values = selection.get(key, [])
        result[key] = len(values) if isinstance(values, list) else 0
    return result


def list_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"missing JSON input: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def render_markdown(evaluation: dict[str, Any]) -> str:
    lines = [
        "# Three-Layer Feedback Evaluation",
        "",
        f"- Target: `{evaluation.get('target', '')}`",
        f"- Rounds: `{evaluation.get('summary', {}).get('round_count', 0)}`",
        f"- Transitions: `{evaluation.get('summary', {}).get('transition_count', 0)}`",
        "",
        "## Round Coverage",
        "",
        "| Round | Cases | Open gaps | Uncovered lines | Overall | Line | Toggle | Branch | Expr | Functional gap |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in evaluation.get("rounds", []):
        lines.append(
            "| {name} | {cases} | {gaps} | {uncovered} | {overall} | {line} | "
            "{toggle} | {branch} | {expr} | {functional} |".format(
                name=row.get("name", ""),
                cases=row.get("cases", 0),
                gaps=row.get("open_gap_count", 0),
                uncovered=row.get("uncovered_lines", 0),
                overall=format_coverage(row.get("overall", {})),
                line=format_coverage(row.get("line", {})),
                toggle=format_coverage(row.get("toggle", {})),
                branch=format_coverage(row.get("branch", {})),
                expr=format_coverage(row.get("expression", {})),
                functional=escape_table(format_functional_gap(row.get("functional_uncovered", {}))),
            )
        )

    lines.extend(
        [
            "",
            "## Transitions",
            "",
            "| Transition | Overall | Lines | Targeted gaps | Resolved | Improved | Stale | Best direction | Decisions |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for item in evaluation.get("transitions", []):
        layer2 = item.get("layer2", {})
        layer3 = item.get("layer3", {})
        lines.append(
            "| {name} | {overall} | {lines:+d} | {targeted} | {resolved} | {improved} | "
            "{stale} | {best} | {decisions} |".format(
                name=f"{item.get('from', '')}->{item.get('to', '')}",
                overall=format_delta(item.get("coverage_delta", {}).get("overall")),
                lines=int(item.get("coverage_delta", {}).get("uncovered_lines", 0)),
                targeted=layer2.get("targeted_gap_count", 0),
                resolved=layer2.get("resolved_targeted_gap_count", 0),
                improved=layer2.get("improved_targeted_gap_count", 0),
                stale=layer2.get("stale_targeted_gap_count", 0),
                best=layer3.get("best_direction") or "",
                decisions=escape_table(format_counts(layer3.get("decision_counts", {}))),
            )
        )

    lines.extend(
        [
            "",
            "## Layer 1 Planning",
            "",
            "| Transition | Directives | Heuristic | Complex LLM | Skipped | Blocked | Source |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in evaluation.get("transitions", []):
        layer1 = item.get("layer1", {})
        counts = layer1.get("gap_selection_counts", {})
        lines.append(
            "| {name} | {directives} | {heuristic} | {complex} | {skipped} | {blocked} | {source} |".format(
                name=f"{item.get('from', '')}->{item.get('to', '')}",
                directives=layer1.get("directive_count", 0),
                heuristic=counts.get("heuristic", 0),
                complex=counts.get("complex_for_llm", 0),
                skipped=counts.get("skipped", 0),
                blocked=counts.get("blocked", 0),
                source=escape_table(str(layer1.get("source", ""))),
            )
        )

    notes = evaluation_notes(evaluation)
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def evaluation_notes(evaluation: dict[str, Any]) -> list[str]:
    notes = []
    for item in evaluation.get("transitions", []):
        attribution = item.get("layer3", {}).get("attribution")
        if attribution == "mixed_proportional":
            notes.append(
                f"{item.get('from', '')}->{item.get('to', '')}: Layer 3 used proportional attribution because multiple directions generated cases."
            )
    return notes


def format_coverage(value: Any) -> str:
    if not isinstance(value, dict) or value.get("coverage") is None:
        return "n/a"
    return f"{float(value['coverage']) * 100.0:.3f}%"


def format_delta(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100.0:+.3f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(f"{name}={count}" for name, count in sorted(value.items()))


def format_functional_gap(value: Any) -> str:
    if not value:
        return "none"
    parts = []
    if isinstance(value, dict):
        for section, gaps in sorted(value.items()):
            if isinstance(gaps, dict):
                for name, missing in sorted(gaps.items()):
                    parts.append(f"{section}.{name}={missing}")
            else:
                parts.append(f"{section}={gaps}")
    else:
        parts.append(str(value))
    return "; ".join(parts)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
