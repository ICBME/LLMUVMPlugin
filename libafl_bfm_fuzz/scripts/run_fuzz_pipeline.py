#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
PY_DIR = REPO_ROOT / "libafl_bfm_fuzz" / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_pipeline.harness import close_observation  # noqa: E402
from fuzz_pipeline.run_orchestrator import (  # noqa: E402
    FuzzRunConfig,
    FuzzRunOrchestrator,
    run_coverage_report_pipeline,
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
        result = FuzzRunOrchestrator(coverage_config(args)).coverage_run()
    finally:
        close_observation()
    print(f"coverage replay finished for {args.target}: returncode={result.returncode}")
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


if __name__ == "__main__":
    raise SystemExit(main())
