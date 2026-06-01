from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .advisors import maybe_call_llm, propose_directives, validate_directives, write_llm_prompt
from .coverage import build_summary


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
    heuristic = propose_directives(summary)
    prompt = {
        "target": args.target,
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }

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
    args.directives_out.write_text(json.dumps(final_directives, indent=2, sort_keys=True) + "\n")
    write_llm_prompt(args.prompt_out, summary, heuristic)
    print(
        f"coverage feedback: target={args.target} uncovered={summary['uncovered_line_count']} "
        f"directives={len(final_directives['directives'])} source={final_directives['source']}"
    )
    return 0
