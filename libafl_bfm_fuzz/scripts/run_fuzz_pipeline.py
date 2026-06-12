#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
FUZZ_DIR = REPO_ROOT / "libafl_bfm_fuzz"
PY_DIR = FUZZ_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_pipeline.campaign_orchestrator import (  # noqa: E402
    CampaignConfig,
    normalize_campaign_modes,
    run_feedback_campaign_pipeline,
)
from fuzz_pipeline import (  # noqa: E402
    CandidateAcceptanceThresholds,
    CandidateRegressionSettings,
    EvaluationBackends,
    HarnessCandidateRegressionBackend,
    LlmHarnessOptimizerBackend,
    NoopHarnessOptimizerBackend,
    default_harness_plugin_registry,
)
from fuzz_pipeline.harness import close_observation  # noqa: E402
from fuzz_pipeline.harness_plugins import load_harness_plugin_registry  # noqa: E402
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
    parser.add_argument("--run-plan-profile")
    parser.add_argument("--evaluation-out", type=Path)
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
    parser.add_argument("--run-plan-profile")
    parser.add_argument("--campaign-plan-profile")
    parser.add_argument("--round-evaluation", action="store_true")
    parser.add_argument("--campaign-evaluation-out", type=Path)
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
    parser.add_argument(
        "--harness-optimizer-backend",
        choices=("noop", "llm"),
        default=None,
        help="Harness optimizer backend for campaign optimization profiles.",
    )
    parser.add_argument(
        "--harness-candidate-backend",
        choices=("noop", "real"),
        default=None,
        help="Candidate validation backend for optimization validation profiles.",
    )
    parser.add_argument(
        "--harness-optimization-plugin",
        action="append",
        default=[],
        help=(
            "Dynamic harness optimization plugin spec in module:Object form. "
            "Can be repeated; also reads HARNESS_OPTIMIZATION_PLUGINS."
        ),
    )
    parser.add_argument("--candidate-modes")
    parser.add_argument("--candidate-rounds", type=int)
    parser.add_argument("--candidate-iters", type=int)
    parser.add_argument("--candidate-max-seeds", type=int)
    parser.add_argument("--candidate-seed", type=int)
    parser.add_argument("--candidate-max-variant-regressions", type=int)
    parser.add_argument("--candidate-attribution-top-k", type=int)
    parser.add_argument(
        "--candidate-attribution-mode",
        choices=("top_k", "all_actions"),
    )
    parser.add_argument("--candidate-paired-repeats", type=int)
    parser.add_argument("--candidate-repeat-seed-stride", type=int)
    parser.add_argument("--candidate-run-plan-profile")
    parser.add_argument("--candidate-campaign-plan-profile")
    parser.add_argument(
        "--candidate-round-evaluation",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--candidate-matched-baseline",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run a matched no-op candidate campaign before comparing candidate metrics.",
    )
    parser.add_argument("--candidate-max-regressed-metrics", type=int)
    parser.add_argument("--candidate-min-improved-metrics", type=int)
    parser.add_argument("--candidate-max-flaky-metrics", type=int)
    parser.add_argument("--candidate-accepted-statuses")
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
        evaluation = result.get("round_evaluation")
        print(
            f"feedback fuzz: target={args.target} mode=no_feedback "
            f"coverage_returncode={replay.returncode} "
            f"round_manifest={args.round_manifest_out}"
            + (
                f" evaluation={args.evaluation_out}"
                if evaluation is not None
                else ""
            )
        )
    else:
        feedback = result["coverage_feedback"]
        replay = result["feedback_replay"]
        evaluation = result.get("round_evaluation")
        print(
            f"feedback fuzz: target={args.target} "
            f"feedback_corpus={args.feedback_corpus} "
            f"directives={len(feedback.final_directives['directives'])} "
            f"replay_returncode={replay.returncode} "
            f"round_manifest={args.round_manifest_out}"
            + (
                f" evaluation={args.evaluation_out}"
                if evaluation is not None
                else ""
            )
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
            evaluation_backends=feedback_campaign_evaluation_backends(args),
        )
    finally:
        runtime.close()
    round_count = sum(len(mode.get("rounds", [])) for mode in manifest.get("modes", []))
    print(
        f"feedback campaign: target={args.target} "
        f"modes={','.join(manifest.get('mode_names', []))} "
        f"rounds={round_count} "
        f"campaign_manifest={manifest['artifacts']['campaign_manifest']}"
        + (
            f" evaluation={args.campaign_evaluation_out}"
            if args.campaign_evaluation_out is not None
            else ""
        )
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
        run_plan_profile=args.run_plan_profile,
        evaluation_out=args.evaluation_out,
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
        run_plan_profile=args.run_plan_profile,
        campaign_plan_profile=args.campaign_plan_profile,
        round_evaluation=args.round_evaluation,
        campaign_evaluation_out=args.campaign_evaluation_out,
        ignore_functional_coverage=args.ignore_functional_coverage,
        llm_model=args.model,
        require_real_llm=args.require_real_llm,
    )


def feedback_campaign_evaluation_backends(
    args: argparse.Namespace,
) -> EvaluationBackends | None:
    optimizer = selected_harness_optimizer_backend(args)
    candidate = selected_harness_candidate_backend(args)
    plugin_registry = harness_plugin_registry_from_args(args)
    if optimizer is None and candidate is None and plugin_registry is None:
        return None
    return EvaluationBackends(
        harness_optimizer=(
            LlmHarnessOptimizerBackend.from_env()
            if optimizer == "llm"
            else NoopHarnessOptimizerBackend()
            if optimizer == "noop"
            else None
        ),
        harness_candidate_evaluation=(
            HarnessCandidateRegressionBackend(
                settings=candidate_regression_settings_from_args(args),
                plugin_registry=plugin_registry,
            )
            if candidate == "real"
            else None
        ),
        harness_plugin_registry=plugin_registry,
    )


def harness_plugin_registry_from_args(args: argparse.Namespace):
    specs = harness_plugin_specs_from_args(args)
    if not specs:
        return None
    return load_harness_plugin_registry(
        specs,
        base_registry=default_harness_plugin_registry(),
        target=args.target,
        args=args,
    )


def harness_plugin_specs_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    values: list[str] = []
    try:
        config = load_target_config(
            args.target,
            target_config=getattr(args, "target_config", None),
        )
        values.extend(config.harness_optimization_plugins)
    except FileNotFoundError:
        pass
    values.extend(getattr(args, "harness_optimization_plugin", []) or [])
    env_value = os.getenv("HARNESS_OPTIMIZATION_PLUGINS")
    if env_value:
        values.extend(
            item.strip()
            for chunk in env_value.splitlines()
            for item in chunk.split(",")
            if item.strip()
        )
    return tuple(values)


def selected_harness_optimizer_backend(args: argparse.Namespace) -> str | None:
    value = args.harness_optimizer_backend or os.getenv("HARNESS_OPTIMIZER_BACKEND")
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"", "none"}:
        return None
    if value not in {"noop", "llm"}:
        raise SystemExit(f"unknown harness optimizer backend: {value}")
    return value


def selected_harness_candidate_backend(args: argparse.Namespace) -> str | None:
    value = args.harness_candidate_backend or os.getenv("HARNESS_CANDIDATE_BACKEND")
    if value is None and profile_requests_real_candidate_backend(
        args.campaign_plan_profile
    ):
        value = "real"
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"", "none"}:
        return None
    if value not in {"noop", "real"}:
        raise SystemExit(f"unknown harness candidate backend: {value}")
    return value


def profile_requests_real_candidate_backend(profile: str | None) -> bool:
    return profile == "campaign_with_evaluation_and_optimization_real_validation"


def candidate_regression_settings_from_args(
    args: argparse.Namespace,
) -> CandidateRegressionSettings:
    return CandidateRegressionSettings(
        modes=candidate_modes_from_args(args),
        rounds=int_arg(args.candidate_rounds, "HARNESS_CANDIDATE_ROUNDS", 1),
        iters=optional_int_arg(args.candidate_iters, "HARNESS_CANDIDATE_ITERS"),
        max_seeds=optional_int_arg(
            args.candidate_max_seeds,
            "HARNESS_CANDIDATE_MAX_SEEDS",
        ),
        seed=optional_int_arg(args.candidate_seed, "HARNESS_CANDIDATE_SEED"),
        max_variant_regressions=int_arg(
            args.candidate_max_variant_regressions,
            "HARNESS_CANDIDATE_MAX_VARIANT_REGRESSIONS",
            1,
        ),
        attribution_top_k=optional_int_arg(
            args.candidate_attribution_top_k,
            "HARNESS_CANDIDATE_ATTRIBUTION_TOP_K",
        ),
        attribution_mode=(
            args.candidate_attribution_mode
            or os.getenv("HARNESS_CANDIDATE_ATTRIBUTION_MODE")
            or "top_k"
        ),
        paired_repeats=int_arg(
            args.candidate_paired_repeats,
            "HARNESS_CANDIDATE_PAIRED_REPEATS",
            3,
        ),
        repeat_seed_stride=int_arg(
            args.candidate_repeat_seed_stride,
            "HARNESS_CANDIDATE_REPEAT_SEED_STRIDE",
            1,
        ),
        run_plan_profile=(
            args.candidate_run_plan_profile
            or os.getenv("HARNESS_CANDIDATE_RUN_PLAN_PROFILE")
        ),
        campaign_plan_profile=(
            args.candidate_campaign_plan_profile
            or os.getenv("HARNESS_CANDIDATE_CAMPAIGN_PLAN_PROFILE")
            or "campaign_with_evaluation"
        ),
        round_evaluation=bool_arg(
            args.candidate_round_evaluation,
            "HARNESS_CANDIDATE_ROUND_EVALUATION",
            True,
        ),
        matched_baseline=bool_arg(
            args.candidate_matched_baseline,
            "HARNESS_CANDIDATE_MATCHED_BASELINE",
            True,
        ),
        thresholds=CandidateAcceptanceThresholds(
            max_regressed_metric_count=int_arg(
                args.candidate_max_regressed_metrics,
                "HARNESS_CANDIDATE_MAX_REGRESSED_METRICS",
                0,
            ),
            min_improved_metric_count=int_arg(
                args.candidate_min_improved_metrics,
                "HARNESS_CANDIDATE_MIN_IMPROVED_METRICS",
                CandidateAcceptanceThresholds().min_improved_metric_count,
            ),
            max_flaky_metric_count=int_arg(
                args.candidate_max_flaky_metrics,
                "HARNESS_CANDIDATE_MAX_FLAKY_METRICS",
                0,
            ),
            accepted_candidate_statuses=accepted_candidate_statuses(args),
        ),
    )


def candidate_modes_from_args(args: argparse.Namespace) -> tuple[str, ...] | None:
    value = args.candidate_modes or os.getenv("HARNESS_CANDIDATE_MODES")
    if not value:
        return None
    try:
        return normalize_campaign_modes(value)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def accepted_candidate_statuses(args: argparse.Namespace) -> tuple[str, ...]:
    value = (
        args.candidate_accepted_statuses
        or os.getenv("HARNESS_CANDIDATE_ACCEPTED_STATUSES")
        or "passed,ok"
    )
    statuses = tuple(item.strip() for item in value.split(",") if item.strip())
    return statuses or ("passed", "ok")


def optional_int_arg(value: int | None, env_name: str) -> int | None:
    if value is not None:
        return value
    env_value = os.getenv(env_name)
    if not env_value:
        return None
    return int(env_value)


def int_arg(value: int | None, env_name: str, default: int) -> int:
    optional = optional_int_arg(value, env_name)
    return default if optional is None else optional


def bool_arg(value: bool | None, env_name: str, default: bool) -> bool:
    if value is not None:
        return value
    env_value = os.getenv(env_name)
    if env_value is None:
        return default
    return env_value.strip().lower() in {"1", "true", "yes", "on"}


def path_from_cwd(path: Path | None, cwd: Path | None) -> Path | None:
    if path is None or path.is_absolute() or cwd is None:
        return path
    base = cwd if cwd.is_absolute() else Path.cwd() / cwd
    return base / path


if __name__ == "__main__":
    raise SystemExit(main())
