from __future__ import annotations

import argparse
from pathlib import Path

from connector_observe import ObservationContext, observer_from_env
from fuzz_pipeline import CoverageFeedbackConfig, run_coverage_feedback_pipeline


def main() -> int:
    args = parse_args()
    observer = observer_from_env(
        args.observation_out,
        monitoring_path=args.monitoring_out,
    )
    context = ObservationContext.from_env(observer=observer)
    if args.observation_run_id:
        context = ObservationContext(
            run_id=args.observation_run_id,
            round_id=context.round_id,
            stage_id=context.stage_id,
            parent_event_id=context.parent_event_id,
            observer=context.observer,
            strict=context.strict,
        )

    try:
        result = run_coverage_feedback_pipeline(
            CoverageFeedbackConfig(
                target=args.target,
                coverage_info=args.coverage_info,
                coverage_dat=args.coverage_dat,
                functional_coverage=args.functional_coverage,
                ignore_functional_coverage=args.ignore_functional_coverage,
                corpus=args.corpus,
                summary_out=args.summary_out,
                directives_out=args.directives_out,
                prompt_out=args.prompt_out,
                previous_summary=args.previous_summary,
                previous_directives=args.previous_directives,
                previous_gap_feedback=args.previous_gap_feedback,
                previous_mutation_feedback=args.previous_mutation_feedback,
                gap_feedback_out=args.gap_feedback_out,
                mutation_feedback_out=args.mutation_feedback_out,
                llm=args.llm,
                llm_response_out=args.llm_response_out,
                model=args.model,
                topology_out=args.topology_out,
            ),
            context,
        )
    finally:
        observer.close()

    print(
        f"coverage feedback: target={args.target} "
        f"uncovered={result.summary['uncovered_line_count']} "
        f"directives={len(result.final_directives['directives'])} "
        f"source={result.final_directives['source']}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--coverage-dat", type=Path)
    parser.add_argument("--functional-coverage", type=Path)
    parser.add_argument(
        "--ignore-functional-coverage",
        action="store_true",
        help=(
            "Build feedback from RTL code coverage only. Functional coverage files "
            "and corpus fallback functional coverage are ignored."
        ),
    )
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
    parser.add_argument(
        "--observation-out",
        type=Path,
        help=(
            "Optional JSONL connector observation output. If omitted, "
            "CONNECTOR_OBSERVE_OUT may also enable observation."
        ),
    )
    parser.add_argument(
        "--monitoring-out",
        type=Path,
        help=(
            "Optional JSON component monitoring summary. If omitted, "
            "CONNECTOR_MONITOR_OUT may also enable monitoring."
        ),
    )
    parser.add_argument(
        "--topology-out",
        type=Path,
        help="Optional JSON description of the component/connector topology.",
    )
    parser.add_argument(
        "--observation-run-id",
        help="Optional run id for connector observation events.",
    )
    return parser.parse_args()
