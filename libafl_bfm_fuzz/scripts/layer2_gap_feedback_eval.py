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

from fuzz_feedback.feedback_loop import build_gap_feedback  # noqa: E402


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
    previous_gap_feedback = (
        load_json(args.previous_gap_feedback) if args.previous_gap_feedback else None
    )
    mutation_feedback = load_json(args.mutation_feedback) if args.mutation_feedback else None

    feedback = build_gap_feedback(
        previous_summary,
        current_summary,
        directives,
        previous_gap_feedback=previous_gap_feedback,
        mutation_feedback=mutation_feedback,
    )

    target = str(feedback.get("target") or args.target or current_summary.get("target", "target"))
    output_dir = args.out_dir or current_summary_path.parent
    feedback_out = args.feedback_out or output_dir / f"{target}_layer2_gap_feedback.json"
    markdown_out = args.markdown_out or output_dir / f"{target}_layer2_gap_feedback.md"

    write_json(feedback_out, feedback)
    markdown = render_markdown(
        feedback,
        previous_summary_path=previous_summary_path,
        current_summary_path=current_summary_path,
        directives_path=args.directives,
        mutation_feedback_path=args.mutation_feedback,
    )
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.write_text(markdown)

    print(markdown)
    print(f"Wrote {feedback_out}")
    print(f"Wrote {markdown_out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate medium-frequency Layer 2 per-gap feedback from two "
            "coverage summaries and the directives applied between them."
        )
    )
    parser.add_argument("--target", help="Target name, required only when using run dirs.")
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--current-summary", type=Path)
    parser.add_argument("--previous-run-dir", type=Path)
    parser.add_argument("--current-run-dir", type=Path)
    parser.add_argument("--directives", type=Path, required=True)
    parser.add_argument("--previous-gap-feedback", type=Path)
    parser.add_argument("--mutation-feedback", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--feedback-out", type=Path)
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
    *,
    previous_summary_path: Path,
    current_summary_path: Path,
    directives_path: Path,
    mutation_feedback_path: Path | None,
) -> str:
    lines = [
        "# Layer 2 Gap Feedback",
        "",
        f"- Target: `{feedback.get('target', '')}`",
        f"- Previous summary: `{previous_summary_path}`",
        f"- Current summary: `{current_summary_path}`",
        f"- Directives: `{directives_path}`",
    ]
    if mutation_feedback_path is not None:
        lines.append(f"- Mutation feedback: `{mutation_feedback_path}`")

    lines.extend(
        [
            "",
            "## Status Counts",
            "",
            "| Status | Count |",
            "| --- | ---: |",
        ]
    )
    for status, count in feedback.get("status_counts", {}).items():
        lines.append(f"| {status} | {count} |")

    lines.extend(
        [
            "",
            "## Next Actions",
            "",
            "| Action | Count |",
            "| --- | ---: |",
        ]
    )
    for action, count in feedback.get("next_action_counts", {}).items():
        lines.append(f"| {action} | {count} |")

    lines.extend(
        [
            "",
            "## Top Gaps",
            "",
            "| Gap | Status | Action | Kind | Points | Stale | Attempts | Directives |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    gaps = feedback.get("gaps", {})
    if isinstance(gaps, dict) and gaps:
        sorted_gaps = sorted(
            gaps.values(),
            key=lambda gap: (
                action_rank(str(gap.get("next_action", ""))),
                -int(gap.get("priority") or 0),
                str(gap.get("id", "")),
            ),
        )
        for gap in sorted_gaps[:24]:
            directives = ", ".join(str(item) for item in gap.get("current_attempted_directives", []))
            lines.append(
                "| {id} | {status} | {action} | {kind} | {points} | {stale} | "
                "{attempts} | {directives} |".format(
                    id=gap.get("id", ""),
                    status=gap.get("status", ""),
                    action=gap.get("next_action", ""),
                    kind=gap.get("primary_kind", ""),
                    points=gap.get("current_point_count", ""),
                    stale=gap.get("stale_count", 0),
                    attempts=gap.get("attempt_count", 0),
                    directives=directives,
                )
            )
    else:
        lines.append("| none |  |  |  |  |  |  |  |")

    return "\n".join(lines) + "\n"


def action_rank(action: str) -> int:
    order = {
        "escalate_to_llm": 0,
        "try_alternative": 1,
        "retry": 2,
        "plan": 3,
        "continue": 4,
        "deprioritize": 5,
        "done": 6,
    }
    return order.get(action, 9)


if __name__ == "__main__":
    raise SystemExit(main())
