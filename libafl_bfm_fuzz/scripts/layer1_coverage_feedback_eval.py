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

from fuzz_feedback.advisors import build_llm_prompt, propose_directives  # noqa: E402


def main() -> int:
    args = parse_args()
    summary_path = resolve_summary_path(args.summary, args.run_dir, args.target)
    summary = load_json(summary_path)
    gap_feedback = load_json(args.gap_feedback) if args.gap_feedback else None
    mutation_feedback = load_json(args.mutation_feedback) if args.mutation_feedback else None

    directives = propose_directives(
        summary,
        gap_feedback=gap_feedback,
        mutation_feedback=mutation_feedback,
    )
    prompt = build_llm_prompt(
        summary,
        directives,
        gap_feedback=gap_feedback,
        mutation_feedback=mutation_feedback,
    )

    target = str(summary.get("target") or args.target or "target")
    output_dir = args.out_dir or summary_path.parent
    directives_out = args.directives_out or output_dir / f"{target}_layer1_directives.json"
    prompt_out = args.prompt_out or output_dir / f"{target}_layer1_llm_prompt.json"
    plan_out = args.plan_out or output_dir / f"{target}_layer1_plan.json"
    markdown_out = args.markdown_out or output_dir / f"{target}_layer1_plan.md"

    structural_plan = directives.get("rtl_gap_mutation_plan", {})
    write_json(directives_out, directives)
    write_json(prompt_out, prompt)
    write_json(plan_out, structural_plan if isinstance(structural_plan, dict) else {})
    markdown = render_markdown(
        directives,
        structural_plan if isinstance(structural_plan, dict) else {},
        summary_path=summary_path,
        gap_feedback_path=args.gap_feedback,
        mutation_feedback_path=args.mutation_feedback,
    )
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.write_text(markdown)

    print(markdown)
    print(f"Wrote {directives_out}")
    print(f"Wrote {prompt_out}")
    print(f"Wrote {plan_out}")
    print(f"Wrote {markdown_out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate low-frequency Layer 1 coverage-to-mutation planning from a "
            "coverage summary and optional Layer 2/3 feedback state."
        )
    )
    parser.add_argument("--target", help="Target name, required only when using --run-dir.")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--gap-feedback", type=Path)
    parser.add_argument("--mutation-feedback", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--directives-out", type=Path)
    parser.add_argument("--prompt-out", type=Path)
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args()


def resolve_summary_path(explicit: Path | None, run_dir: Path | None, target: str | None) -> Path:
    if explicit is not None:
        return explicit
    if run_dir is not None and target:
        return run_dir / f"{target}_coverage_summary.json"
    raise SystemExit("provide --summary or both --target and --run-dir")


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
    directives: dict[str, Any],
    structural_plan: dict[str, Any],
    *,
    summary_path: Path,
    gap_feedback_path: Path | None,
    mutation_feedback_path: Path | None,
) -> str:
    selection = structural_plan.get("gap_selection", {})
    lines = [
        "# Layer 1 Coverage Feedback Plan",
        "",
        f"- Source: `{directives.get('source', '')}`",
        f"- Summary: `{summary_path}`",
    ]
    if gap_feedback_path is not None:
        lines.append(f"- Gap feedback: `{gap_feedback_path}`")
    if mutation_feedback_path is not None:
        lines.append(f"- Mutation feedback: `{mutation_feedback_path}`")

    lines.extend(
        [
            "",
            "## Gap Selection",
            "",
            "| Bucket | Count |",
            "| --- | ---: |",
        ]
    )
    for bucket in ("heuristic", "complex_for_llm", "skipped", "deprioritized", "blocked"):
        values = selection.get(bucket, [])
        count = len(values) if isinstance(values, list) else 0
        lines.append(f"| {bucket} | {count} |")

    lines.extend(
        [
            "",
            "## Directives",
            "",
            "| Name | Enabled | Weight | Gap IDs | Decision |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for directive in directives.get("directives", []):
        if not isinstance(directive, dict):
            continue
        gap_ids = ", ".join(str(gap_id) for gap_id in directive.get("gap_ids", []))
        lines.append(
            "| {name} | {enabled} | {weight} | {gap_ids} | {decision} |".format(
                name=directive.get("name", ""),
                enabled=directive.get("enabled", True),
                weight=directive.get("weight", ""),
                gap_ids=gap_ids,
                decision=directive.get("feedback_decision", ""),
            )
        )

    complex_gaps = structural_plan.get("complex_gaps", [])
    if isinstance(complex_gaps, list) and complex_gaps:
        lines.extend(["", "## Complex Gaps For LLM", ""])
        for gap in complex_gaps[:12]:
            if isinstance(gap, dict):
                lines.append(
                    "- `{}` `{}` line `{}`".format(
                        gap.get("id", ""),
                        gap.get("primary_kind", ""),
                        gap.get("line", ""),
                    )
                )

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
