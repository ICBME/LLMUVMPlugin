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

from fuzz_feedback.feedback_loop import (  # noqa: E402
    build_mutation_feedback,
    update_mutation_directions,
)


def main() -> int:
    args = parse_args()
    previous_summary_path = resolve_summary_path(
        args.previous_summary,
        args.previous_run_dir,
        args.target,
        "--previous-summary",
    )
    current_summary_path = resolve_summary_path(
        args.current_summary,
        args.current_run_dir,
        args.target,
        "--current-summary",
    )

    previous_summary = load_json(previous_summary_path)
    current_summary = load_json(current_summary_path)
    directives = load_json(args.directives)
    previous_feedback = load_json(args.previous_feedback) if args.previous_feedback else None

    feedback = build_mutation_feedback(
        previous_summary,
        current_summary,
        directives,
        previous_feedback=previous_feedback,
    )
    updated_directives = update_mutation_directions(directives, feedback)

    target = str(feedback.get("target") or args.target or current_summary.get("target", "target"))
    output_dir = args.out_dir or current_summary_path.parent
    feedback_out = args.feedback_out or output_dir / f"{target}_layer3_mutation_feedback.json"
    updated_directives_out = (
        args.updated_directives_out
        or output_dir / f"{target}_layer3_updated_directives.json"
    )
    markdown_out = args.markdown_out or output_dir / f"{target}_layer3_mutation_feedback.md"

    write_json(feedback_out, feedback)
    write_json(updated_directives_out, updated_directives)
    markdown = render_markdown(
        feedback,
        updated_directives,
        previous_summary_path=previous_summary_path,
        current_summary_path=current_summary_path,
        directives_path=args.directives,
    )
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.write_text(markdown)

    print(markdown)
    print(f"Wrote {feedback_out}")
    print(f"Wrote {updated_directives_out}")
    print(f"Wrote {markdown_out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate high-frequency Layer 3 mutation direction feedback from two "
            "coverage summaries and the directives applied between them."
        )
    )
    parser.add_argument("--target", help="Target name, required only when using run dirs.")
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--current-summary", type=Path)
    parser.add_argument("--previous-run-dir", type=Path)
    parser.add_argument("--current-run-dir", type=Path)
    parser.add_argument("--directives", type=Path, required=True)
    parser.add_argument("--previous-feedback", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--feedback-out", type=Path)
    parser.add_argument("--updated-directives-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args()


def resolve_summary_path(
    explicit: Path | None,
    run_dir: Path | None,
    target: str | None,
    label: str,
) -> Path:
    if explicit is not None:
        return explicit
    if run_dir is not None and target:
        return run_dir / f"{target}_coverage_summary.json"
    raise SystemExit(f"provide {label} or both --target and the matching run-dir")


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


def render_markdown(
    feedback: dict[str, Any],
    updated_directives: dict[str, Any],
    *,
    previous_summary_path: Path,
    current_summary_path: Path,
    directives_path: Path,
) -> str:
    aggregate = feedback.get("aggregate_delta", {})
    lines = [
        "# Layer 3 Mutation Feedback",
        "",
        f"- Target: `{feedback.get('target', '')}`",
        f"- Attribution: `{feedback.get('attribution', '')}`",
        f"- Previous summary: `{previous_summary_path}`",
        f"- Current summary: `{current_summary_path}`",
        f"- Directives: `{directives_path}`",
        "",
        "## Aggregate Delta",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Structural resolved points | {aggregate.get('structural_resolved_point_count', 0)} |",
        f"| Resolved RTL gaps | {aggregate.get('resolved_gap_count', 0)} |",
        f"| Functional new bins | {aggregate.get('functional_new_bin_count', 0)} |",
        f"| Structural coverage delta | {format_percent_delta(aggregate.get('structural_coverage_delta'))} |",
        f"| Uncovered line delta | {aggregate.get('uncovered_line_delta', 0)} |",
        f"| New generated cases | {aggregate.get('total_new_generated_cases', 0)} |",
        "",
        "## Mutation Directions",
        "",
        "| Direction | New cases | Replay drop | Resolved points | Resolved gaps | New func bins | Score | Decision | Weight |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]

    directions = feedback.get("directions", {})
    if isinstance(directions, dict) and directions:
        for name, direction in sorted(
            directions.items(),
            key=lambda item: (-float(item[1].get("score", 0.0)), str(item[0])),
        ):
            lines.append(
                "| {name} | {new_cases} | {replay_drop} | {points} | {gaps} | "
                "{func_bins} | {score} | {decision} | {weight} |".format(
                    name=name,
                    new_cases=direction.get("new_generated_cases", 0),
                    replay_drop=direction.get("replay_drop", 0),
                    points=direction.get("structural_resolved_points", 0),
                    gaps=direction.get("resolved_gap_count", 0),
                    func_bins=direction.get("functional_new_bins", 0),
                    score=direction.get("score", 0),
                    decision=direction.get("decision", ""),
                    weight=direction.get("updated_weight", ""),
                )
            )
    else:
        lines.append("| none | 0 | 0 | 0 | 0 | 0 | 0 | inactive |  |")

    disabled = [
        directive.get("name")
        for directive in updated_directives.get("directives", [])
        if isinstance(directive, dict) and directive.get("enabled") is False
    ]
    if disabled:
        lines.extend(["", "## Temporarily Suppressed", ""])
        lines.extend(f"- `{name}`" for name in disabled if name)

    return "\n".join(lines) + "\n"


def format_percent_delta(value: Any) -> str:
    try:
        return f"{float(value) * 100.0:+.3f}%"
    except (TypeError, ValueError):
        return "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
