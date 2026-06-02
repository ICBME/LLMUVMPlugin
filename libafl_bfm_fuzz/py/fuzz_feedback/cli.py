from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .advisors import (
    build_llm_prompt,
    maybe_call_llm,
    propose_directives,
    validate_directives,
    write_llm_prompt,
)
from .coverage import build_summary
from .feedback_loop import build_gap_feedback, build_mutation_feedback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--coverage-dat", type=Path)
    parser.add_argument("--functional-coverage", type=Path)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--directives-out", type=Path, required=True)
    parser.add_argument("--prompt-out", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--previous-directives", type=Path)
    parser.add_argument("--previous-gap-feedback", type=Path)
    parser.add_argument("--previous-mutation-feedback", type=Path)
    parser.add_argument("--gap-feedback-out", type=Path)
    parser.add_argument("--mutation-feedback-out", type=Path)
    parser.add_argument("--llm", action="store_true", help="Call an LLM when OPENAI_API_KEY is available")
    parser.add_argument("--llm-response-out", type=Path)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    summary = build_summary(
        args.target,
        args.coverage_info,
        args.corpus,
        coverage_dat=args.coverage_dat,
        functional_coverage=args.functional_coverage,
    )

    previous_summary = read_optional_json(args.previous_summary)
    previous_directives = read_optional_json(args.previous_directives)
    previous_gap_feedback = read_optional_json(args.previous_gap_feedback)
    previous_mutation_feedback = read_optional_json(args.previous_mutation_feedback)
    applied_directives = previous_directives or {"directives": []}

    mutation_feedback = None
    gap_feedback = None
    if previous_summary is not None:
        mutation_feedback = build_mutation_feedback(
            previous_summary,
            summary,
            applied_directives,
            previous_feedback=previous_mutation_feedback,
        )
        gap_feedback = build_gap_feedback(
            previous_summary,
            summary,
            applied_directives,
            previous_gap_feedback=previous_gap_feedback,
            mutation_feedback=mutation_feedback,
        )

    heuristic = propose_directives(
        summary,
        gap_feedback=gap_feedback,
        mutation_feedback=mutation_feedback,
    )
    prompt = build_llm_prompt(
        summary,
        heuristic,
        gap_feedback=gap_feedback,
        mutation_feedback=mutation_feedback,
    )

    final_directives = heuristic
    if args.llm:
        try:
            llm_value = maybe_call_llm(prompt, args.model)
            if llm_value is not None:
                if args.llm_response_out:
                    args.llm_response_out.parent.mkdir(parents=True, exist_ok=True)
                    args.llm_response_out.write_text(json.dumps(llm_value, indent=2, sort_keys=True) + "\n")
                final_directives = validate_directives(args.target, llm_value)
            else:
                final_directives["source"] = "heuristic; OPENAI_API_KEY not set"
        except Exception as exc:  # noqa: BLE001 - keep fuzz loop moving with heuristic fallback
            final_directives = heuristic
            final_directives["source"] = f"heuristic; LLM failed: {exc}"
            print(f"warning: LLM feedback failed, using heuristic directives: {exc}", file=sys.stderr)

    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if args.gap_feedback_out and gap_feedback is not None:
        args.gap_feedback_out.parent.mkdir(parents=True, exist_ok=True)
        args.gap_feedback_out.write_text(json.dumps(gap_feedback, indent=2, sort_keys=True) + "\n")
    if args.mutation_feedback_out and mutation_feedback is not None:
        args.mutation_feedback_out.parent.mkdir(parents=True, exist_ok=True)
        args.mutation_feedback_out.write_text(
            json.dumps(mutation_feedback, indent=2, sort_keys=True) + "\n"
        )
    args.directives_out.write_text(json.dumps(final_directives, indent=2, sort_keys=True) + "\n")
    write_llm_prompt(args.prompt_out, summary, heuristic)
    print(
        f"coverage feedback: target={args.target} uncovered={summary['uncovered_line_count']} "
        f"directives={len(final_directives['directives'])} source={final_directives['source']}"
    )
    return 0


def read_optional_json(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else None
