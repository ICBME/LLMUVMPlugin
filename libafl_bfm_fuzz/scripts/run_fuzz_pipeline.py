#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
FUZZ_DIR = REPO_ROOT / "libafl_bfm_fuzz"
PY_DIR = FUZZ_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_pipeline.campaign_orchestrator import (  # noqa: E402
    CampaignConfig,
    normalize_campaign_modes,
    run_feedback_campaign_pipeline,
)
from fuzz_pipeline.harness import close_observation  # noqa: E402
from fuzz_pipeline.observation import ObservationRuntime  # noqa: E402
from fuzz_pipeline.run_orchestrator import (  # noqa: E402
    FuzzRunConfig,
    FuzzRunOrchestrator,
    run_coverage_feedback_stage_pipeline,
    run_coverage_report_pipeline,
    run_feedback_fuzz_pipeline,
    run_generate_corpus_pipeline,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "generate-corpus":
        return generate_corpus(args)
    if args.command == "coverage-run":
        return coverage_run(args)
    if args.command == "coverage-report":
        return coverage_report(args)
    if args.command == "coverage-feedback":
        return coverage_feedback(args)
    if args.command == "feedback-fuzz":
        return feedback_fuzz(args)
    if args.command == "feedback-campaign":
        return feedback_campaign(args)
    raise SystemExit(f"unknown command: {args.command}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run libafl_bfm_fuzz pipeline stages.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-corpus")
    generate.add_argument("--target", required=True)
    generate.add_argument("--target-config", type=Path)
    generate.add_argument("--corpus-out", type=Path, required=True)
    generate.add_argument("--libafl-manifest", type=Path, required=True)
    generate.add_argument("--directives", type=Path)
    generate.add_argument("--iters", type=int, default=256)
    generate.add_argument("--max-seeds", type=int, default=32)
    generate.add_argument("--seed", type=int, default=1)
    generate.add_argument("--cargo", default="cargo")
    generate.add_argument("--topology-out", type=Path)
    generate.add_argument("--cwd", type=Path)

    coverage_run_parser = subparsers.add_parser("coverage-run")
    add_coverage_args(coverage_run_parser)

    coverage_report_parser = subparsers.add_parser("coverage-report")
    add_coverage_args(coverage_report_parser)

    coverage_feedback_parser = subparsers.add_parser("coverage-feedback")
    add_coverage_feedback_args(coverage_feedback_parser)

    feedback_fuzz_parser = subparsers.add_parser("feedback-fuzz")
    add_feedback_fuzz_args(feedback_fuzz_parser)

    feedback_campaign_parser = subparsers.add_parser("feedback-campaign")
    add_feedback_campaign_args(feedback_campaign_parser)
    return parser.parse_args(argv)


def add_coverage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True)
    parser.add_argument("--target-config", type=Path)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--libafl-manifest", type=Path, required=True)
    parser.add_argument("--directives", type=Path)
    parser.add_argument("--iters", type=int, default=256)
    parser.add_argument("--max-seeds", type=int, default=32)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--make", default="make")
    parser.add_argument("--verilog-sources")
    parser.add_argument("--toplevel")
    parser.add_argument("--coverage-dir", type=Path, required=True)
    parser.add_argument("--coverage-dat", type=Path, required=True)
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--coverage-annotated", type=Path, required=True)
    parser.add_argument("--functional-coverage", type=Path)
    parser.add_argument("--verilator-coverage", default="verilator_coverage")
    parser.add_argument("--topology-out", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument(
        "--make-var",
        action="append",
        default=[],
        help="Extra Makefile variable assignment, for example EXTRA_ARGS=-Wno-UNOPTFLAT.",
    )


def add_coverage_feedback_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True)
    parser.add_argument("--target-config", type=Path)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--libafl-manifest", type=Path, default=FUZZ_DIR / "Cargo.toml")
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--coverage-dat", type=Path)
    parser.add_argument("--functional-coverage", type=Path)
    parser.add_argument("--ignore-functional-coverage", action="store_true")
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--directives-out", type=Path, required=True)
    parser.add_argument("--heuristic-directives-out", type=Path)
    parser.add_argument("--prompt-out", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--previous-directives", type=Path)
    parser.add_argument("--previous-gap-feedback", type=Path)
    parser.add_argument("--previous-mutation-feedback", type=Path)
    parser.add_argument("--gap-feedback-out", type=Path)
    parser.add_argument("--mutation-feedback-out", type=Path)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-response-out", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--topology-out", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--observation-out", type=Path)
    parser.add_argument("--monitoring-out", type=Path)
    parser.add_argument("--observation-run-id")
    parser.add_argument("--round-id")


def add_feedback_fuzz_args(parser: argparse.ArgumentParser) -> None:
    add_coverage_args(parser)
    parser.add_argument("--feedback-functional-coverage", type=Path)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--directives-out", type=Path, required=True)
    parser.add_argument("--heuristic-directives-out", type=Path)
    parser.add_argument("--prompt-out", type=Path, required=True)
    parser.add_argument("--previous-summary", type=Path)
    parser.add_argument("--previous-directives", type=Path)
    parser.add_argument("--previous-gap-feedback", type=Path)
    parser.add_argument("--previous-mutation-feedback", type=Path)
    parser.add_argument("--gap-feedback-out", type=Path)
    parser.add_argument("--mutation-feedback-out", type=Path)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--llm-response-out", type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--feedback-corpus", type=Path, required=True)
    parser.add_argument("--round-manifest-out", type=Path)
    parser.add_argument("--mode", default="feedback_fuzz")
    parser.add_argument("--round-id")
    parser.add_argument("--ignore-functional-coverage", action="store_true")
    parser.add_argument("--observation-out", type=Path)
    parser.add_argument("--monitoring-out", type=Path)
    parser.add_argument("--observation-run-id")


def add_feedback_campaign_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True)
    parser.add_argument("--target-config", type=Path)
    parser.add_argument("--libafl-manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--campaign-manifest-out", type=Path)
    parser.add_argument("--modes", default="heuristic_feedback")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--iters", type=int, default=256)
    parser.add_argument("--max-seeds", type=int, default=32)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--make", default="make")
    parser.add_argument("--verilog-sources")
    parser.add_argument("--toplevel")
    parser.add_argument("--verilator-coverage", default="verilator_coverage")
    parser.add_argument("--topology-out", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument(
        "--make-var",
        action="append",
        default=[],
        help="Extra Makefile variable assignment, for example EXTRA_ARGS=-Wno-UNOPTFLAT.",
    )
    parser.add_argument("--ignore-functional-coverage", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--require-real-llm", action="store_true")
    parser.add_argument("--observation-out", type=Path)
    parser.add_argument("--monitoring-out", type=Path)
    parser.add_argument("--observation-run-id")


def generate_corpus(args: argparse.Namespace) -> int:
    try:
        cases = run_generate_corpus_pipeline(
            FuzzRunConfig(
                target=args.target,
                target_config=args.target_config,
                corpus=args.corpus_out,
                libafl_manifest=args.libafl_manifest,
                directives=args.directives,
                iters=args.iters,
                max_seeds=args.max_seeds,
                seed=args.seed,
                cargo=args.cargo,
                cwd=args.cwd,
                topology_out=args.topology_out,
            )
        )
    finally:
        close_observation()
    print(f"generated and validated {len(cases)} {args.target} cases in {args.corpus_out}")
    return 0


def coverage_run(args: argparse.Namespace) -> int:
    try:
        result = FuzzRunOrchestrator(coverage_config(args)).coverage_run_pipeline()
    finally:
        close_observation()
    replay = result["coverage_run"]
    print(f"coverage replay finished for {args.target}: returncode={replay.returncode}")
    return 0


def coverage_report(args: argparse.Namespace) -> int:
    try:
        run_coverage_report_pipeline(coverage_config(args))
    finally:
        close_observation()
    print(
        f"coverage report finished for {args.target}: "
        f"dat={args.coverage_dat} info={args.coverage_info}"
    )
    return 0


def coverage_feedback(args: argparse.Namespace) -> int:
    runtime = ObservationRuntime.from_env(
        observation_out=path_from_cwd(args.observation_out, args.cwd),
        monitoring_out=path_from_cwd(args.monitoring_out, args.cwd),
        topology_out=path_from_cwd(args.topology_out, args.cwd),
        run_id=args.observation_run_id,
        round_id=args.round_id,
    )
    try:
        result = run_coverage_feedback_stage_pipeline(
            coverage_feedback_config(args, topology_out=runtime.topology_out),
            observation_context=runtime.context,
        )
    finally:
        runtime.close()
    print(
        f"coverage feedback: target={args.target} "
        f"uncovered={result.summary['uncovered_line_count']} "
        f"directives={len(result.final_directives['directives'])} "
        f"source={result.final_directives['source']}"
    )
    return 0


def feedback_fuzz(args: argparse.Namespace) -> int:
    runtime = ObservationRuntime.from_env(
        observation_out=path_from_cwd(args.observation_out, args.cwd),
        monitoring_out=path_from_cwd(args.monitoring_out, args.cwd),
        topology_out=path_from_cwd(args.topology_out, args.cwd),
        run_id=args.observation_run_id,
        round_id=args.round_id,
    )
    try:
        result = run_feedback_fuzz_pipeline(
            feedback_fuzz_config(args, topology_out=runtime.topology_out),
            observation_context=runtime.context,
        )
    finally:
        runtime.close()
    if args.mode == "no_feedback":
        replay = result["coverage_run"]
        print(
            f"feedback fuzz: target={args.target} mode=no_feedback "
            f"coverage_returncode={replay.returncode} "
            f"round_manifest={args.round_manifest_out}"
        )
    else:
        feedback = result["coverage_feedback"]
        replay = result["feedback_replay"]
        print(
            f"feedback fuzz: target={args.target} "
            f"feedback_corpus={args.feedback_corpus} "
            f"directives={len(feedback.final_directives['directives'])} "
            f"replay_returncode={replay.returncode} "
            f"round_manifest={args.round_manifest_out}"
        )
    return 0


def feedback_campaign(args: argparse.Namespace) -> int:
    runtime = ObservationRuntime.from_env(
        observation_out=path_from_cwd(args.observation_out, args.cwd),
        monitoring_out=path_from_cwd(args.monitoring_out, args.cwd),
        topology_out=path_from_cwd(args.topology_out, args.cwd),
        run_id=args.observation_run_id,
    )
    try:
        manifest = run_feedback_campaign_pipeline(
            feedback_campaign_config(args, topology_out=runtime.topology_out),
            observation_context=runtime.context,
        )
    finally:
        runtime.close()
    round_count = sum(len(mode.get("rounds", [])) for mode in manifest.get("modes", []))
    print(
        f"feedback campaign: target={args.target} "
        f"modes={','.join(manifest.get('mode_names', []))} "
        f"rounds={round_count} "
        f"campaign_manifest={manifest['artifacts']['campaign_manifest']}"
    )
    return 0


def coverage_config(args: argparse.Namespace) -> FuzzRunConfig:
    return FuzzRunConfig(
        target=args.target,
        target_config=args.target_config,
        corpus=args.corpus,
        libafl_manifest=args.libafl_manifest,
        directives=args.directives,
        iters=args.iters,
        max_seeds=args.max_seeds,
        seed=args.seed,
        cargo=args.cargo,
        cwd=args.cwd,
        topology_out=args.topology_out,
        make=args.make,
        verilog_sources=args.verilog_sources,
        toplevel=args.toplevel,
        coverage_dir=args.coverage_dir,
        coverage_dat=args.coverage_dat,
        coverage_info=args.coverage_info,
        coverage_annotated=args.coverage_annotated,
        functional_coverage=args.functional_coverage,
        verilator_coverage=args.verilator_coverage,
        extra_make_vars=tuple(args.make_var),
    )


def coverage_feedback_config(
    args: argparse.Namespace,
    *,
    topology_out: Path | None = None,
) -> FuzzRunConfig:
    return FuzzRunConfig(
        target=args.target,
        target_config=args.target_config,
        corpus=args.corpus,
        libafl_manifest=args.libafl_manifest,
        cwd=args.cwd,
        topology_out=topology_out,
        coverage_info=args.coverage_info,
        coverage_dat=args.coverage_dat,
        functional_coverage=args.functional_coverage,
        summary_out=args.summary_out,
        directives_out=args.directives_out,
        heuristic_directives_out=args.heuristic_directives_out,
        prompt_out=args.prompt_out,
        previous_summary=args.previous_summary,
        previous_directives=args.previous_directives,
        previous_gap_feedback=args.previous_gap_feedback,
        previous_mutation_feedback=args.previous_mutation_feedback,
        gap_feedback_out=args.gap_feedback_out,
        mutation_feedback_out=args.mutation_feedback_out,
        llm_response_out=args.llm_response_out,
        ignore_functional_coverage=args.ignore_functional_coverage,
        llm=args.llm,
        llm_model=args.model,
    )


def feedback_fuzz_config(
    args: argparse.Namespace,
    *,
    topology_out: Path | None = None,
) -> FuzzRunConfig:
    return FuzzRunConfig(
        target=args.target,
        target_config=args.target_config,
        corpus=args.corpus,
        libafl_manifest=args.libafl_manifest,
        directives=args.directives,
        iters=args.iters,
        max_seeds=args.max_seeds,
        seed=args.seed,
        cargo=args.cargo,
        cwd=args.cwd,
        topology_out=topology_out,
        make=args.make,
        verilog_sources=args.verilog_sources,
        toplevel=args.toplevel,
        coverage_dir=args.coverage_dir,
        coverage_dat=args.coverage_dat,
        coverage_info=args.coverage_info,
        coverage_annotated=args.coverage_annotated,
        functional_coverage=args.functional_coverage,
        feedback_functional_coverage=args.feedback_functional_coverage,
        verilator_coverage=args.verilator_coverage,
        extra_make_vars=tuple(args.make_var),
        summary_out=args.summary_out,
        directives_out=args.directives_out,
        heuristic_directives_out=args.heuristic_directives_out,
        prompt_out=args.prompt_out,
        previous_summary=args.previous_summary,
        previous_directives=args.previous_directives,
        previous_gap_feedback=args.previous_gap_feedback,
        previous_mutation_feedback=args.previous_mutation_feedback,
        gap_feedback_out=args.gap_feedback_out,
        mutation_feedback_out=args.mutation_feedback_out,
        llm_response_out=args.llm_response_out,
        feedback_corpus=args.feedback_corpus,
        ignore_functional_coverage=args.ignore_functional_coverage,
        llm=args.llm,
        llm_model=args.model,
        mode=args.mode,
        round_id=args.round_id,
        round_manifest_out=args.round_manifest_out,
        observation_out=path_from_cwd(args.observation_out, args.cwd),
        monitoring_out=path_from_cwd(args.monitoring_out, args.cwd),
    )


def feedback_campaign_config(
    args: argparse.Namespace,
    *,
    topology_out: Path | None = None,
) -> CampaignConfig:
    try:
        modes = normalize_campaign_modes(args.modes)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return CampaignConfig(
        target=args.target,
        target_config=args.target_config,
        out_dir=args.out_dir,
        libafl_manifest=args.libafl_manifest,
        modes=modes,
        rounds=args.rounds,
        iters=args.iters,
        max_seeds=args.max_seeds,
        seed=args.seed,
        cargo=args.cargo,
        make=args.make,
        verilog_sources=args.verilog_sources,
        toplevel=args.toplevel,
        verilator_coverage=args.verilator_coverage,
        extra_make_vars=tuple(args.make_var),
        cwd=args.cwd,
        topology_out=topology_out,
        observation_out=path_from_cwd(args.observation_out, args.cwd),
        monitoring_out=path_from_cwd(args.monitoring_out, args.cwd),
        campaign_manifest_out=args.campaign_manifest_out,
        ignore_functional_coverage=args.ignore_functional_coverage,
        llm_model=args.model,
        require_real_llm=args.require_real_llm,
    )


def path_from_cwd(path: Path | None, cwd: Path | None) -> Path | None:
    if path is None or path.is_absolute() or cwd is None:
        return path
    base = cwd if cwd.is_absolute() else Path.cwd() / cwd
    return base / path


if __name__ == "__main__":
    raise SystemExit(main())
