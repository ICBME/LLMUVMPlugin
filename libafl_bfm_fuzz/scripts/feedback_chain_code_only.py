#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import feedback_chain_compare as chain


DEFAULT_OUT_DIR = chain.FUZZ_DIR / "coverage" / "feedback_chain_code_only"


def main() -> int:
    args = parse_args()
    targets = chain.select_targets(args.target)
    modes = chain.normalize_modes(args.modes)
    out_dir = args.out_dir.resolve()

    if args.clean and out_dir.exists() and not args.reuse_existing:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    use_uv = chain.resolve_uv_mode(args.use_uv)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if args.env_file:
        load_env_file(args.env_file, env)

    comparisons = []
    for spec in targets:
        target_root = out_dir / spec.target
        target_root.mkdir(parents=True, exist_ok=True)
        if not args.reuse_existing:
            run_target_campaign(
                spec,
                modes=modes,
                rounds=args.rounds,
                target_root=target_root,
                use_uv=use_uv,
                libafl_iters=args.libafl_iters,
                libafl_max_seeds=args.libafl_max_seeds,
                libafl_seed=args.libafl_seed,
                require_real_llm=args.require_real_llm,
                llm_model=args.llm_model,
                env=env,
                quiet=args.quiet,
            )
        comparisons.append(
            chain.build_target_comparison(
                spec.target,
                modes,
                args.rounds,
                target_root,
                ignore_functional=True,
            )
        )

    comparison = {
        "schema_version": 1,
        "scope": "rtl_code_feedback_only",
        "functional_coverage_ignored": True,
        "rounds": args.rounds,
        "modes": list(modes),
        "targets": comparisons,
    }
    comparison_json = out_dir / "feedback_chain_code_only_comparison.json"
    comparison_md = out_dir / "feedback_chain_code_only_comparison.md"
    chain.write_json(comparison_json, comparison)
    comparison_md.write_text(render_comparison_markdown(comparison))

    coverage_report = chain.build_code_coverage_report(comparison)
    coverage_report["scope"] = "rtl_code_feedback_and_evaluation_only"
    coverage_report["functional_coverage_ignored"] = True
    coverage_json = out_dir / "feedback_chain_code_only_coverage.json"
    coverage_md = out_dir / "feedback_chain_code_only_coverage.md"
    chain.write_json(coverage_json, coverage_report)
    coverage_md.write_text(render_coverage_markdown(coverage_report))

    print(comparison_md.read_text())
    print(f"Wrote {comparison_json}")
    print(f"Wrote {comparison_md}")
    print(f"Wrote {coverage_json}")
    print(f"Wrote {coverage_md}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and compare multi-round fuzz campaigns using RTL code coverage "
            "feedback only. UVM functional coverage is generated at replay time if "
            "the environment emits it, but it is ignored by the feedback loop."
        )
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=["all", *chain.TARGETS.keys()],
        default=None,
        help="Target to run. Repeat for multiple targets. Default: all.",
    )
    parser.add_argument(
        "--modes",
        default="no_feedback,heuristic_feedback,llm_feedback",
        help="Comma-separated modes to compare.",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--libafl-iters", type=int, default=256)
    parser.add_argument("--libafl-max-seeds", type=int, default=32)
    parser.add_argument("--libafl-seed", type=int, default=1)
    parser.add_argument(
        "--use-uv",
        choices=["auto", "always", "never"],
        default="auto",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional dotenv file used for LLM settings. Values are not printed.",
    )
    parser.add_argument("--llm-model", default=None)
    parser.add_argument(
        "--require-real-llm",
        action="store_true",
        help="Fail if llm_feedback falls back to heuristic directives.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Do not run make; summarize existing mode/round artifacts only.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        help="Keep existing output directory before running.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.set_defaults(clean=True)
    return parser.parse_args()


def run_target_campaign(
    spec: chain.TargetSpec,
    *,
    modes: tuple[str, ...],
    rounds: int,
    target_root: Path,
    use_uv: bool,
    libafl_iters: int,
    libafl_max_seeds: int,
    libafl_seed: int,
    require_real_llm: bool,
    llm_model: str | None,
    env: dict[str, str],
    quiet: bool,
) -> None:
    if rounds < 1:
        raise SystemExit("--rounds must be >= 1")
    for mode in modes:
        previous: chain.RoundPaths | None = None
        for index in range(rounds):
            paths = chain.round_paths(spec.target, mode, index, target_root)
            print(f"\n== {spec.target}: {mode} code-only round {index:02d} ==")
            run_round(
                spec,
                paths,
                previous=previous if mode in chain.FEEDBACK_MODES else None,
                use_uv=use_uv,
                libafl_iters=libafl_iters,
                libafl_max_seeds=libafl_max_seeds,
                libafl_seed=libafl_seed + index,
                llm_feedback=mode == "llm_feedback",
                llm_model=llm_model,
                env=env,
                quiet=quiet,
            )
            if mode == "llm_feedback" and require_real_llm:
                source = chain.directive_source(paths.directives)
                if not chain.is_real_llm_source(source):
                    raise SystemExit(
                        f"{spec.target} {mode} round {index:02d}: expected real LLM "
                        f"directives, got {source!r}"
                    )
            previous = paths


def run_round(
    spec: chain.TargetSpec,
    paths: chain.RoundPaths,
    *,
    previous: chain.RoundPaths | None,
    use_uv: bool,
    libafl_iters: int,
    libafl_max_seeds: int,
    libafl_seed: int,
    llm_feedback: bool,
    llm_model: str | None,
    env: dict[str, str],
    quiet: bool,
) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_functional_output(paths)

    coverage_cmd = coverage_report_command(
        spec,
        paths,
        previous=previous,
        use_uv=use_uv,
        libafl_iters=libafl_iters,
        libafl_max_seeds=libafl_max_seeds,
        libafl_seed=libafl_seed,
    )
    chain.run_logged(
        coverage_cmd,
        chain.REPO_ROOT,
        paths.run_dir / "coverage_report.log",
        env,
        quiet=quiet,
    )

    feedback_cmd = feedback_command(
        paths,
        previous=previous,
        use_uv=use_uv,
        llm_feedback=llm_feedback,
        llm_model=llm_model,
    )
    chain.run_logged(
        feedback_cmd,
        chain.REPO_ROOT,
        paths.run_dir / "coverage_feedback_code_only.log",
        env,
        quiet=quiet,
    )


def coverage_report_command(
    spec: chain.TargetSpec,
    paths: chain.RoundPaths,
    *,
    previous: chain.RoundPaths | None,
    use_uv: bool,
    libafl_iters: int,
    libafl_max_seeds: int,
    libafl_seed: int,
) -> list[str]:
    cmd = chain.make_command(use_uv)
    cmd.extend(
        [
            "-C",
            str(chain.FUZZ_DIR),
            f"TARGET={spec.target}",
            f"LIBAFL_ITERS={libafl_iters}",
            f"LIBAFL_MAX_SEEDS={libafl_max_seeds}",
            f"LIBAFL_SEED={libafl_seed}",
            f"COVERAGE_DIR={paths.run_dir}",
            f"COVERAGE_INFO={paths.coverage_info}",
            f"COVERAGE_DAT={paths.coverage_dat}",
            f"FUZZ_CORPUS={paths.corpus}",
            f"UVM_FUNCTIONAL_COVERAGE_OUT={paths.ignored_functional}",
            f"VERILOG_SOURCES={chain.rtl_sources(spec)}",
            f"TOPLEVEL={spec.toplevel}",
        ]
    )
    if spec.extra_args:
        cmd.append(f"EXTRA_ARGS={spec.extra_args}")
    if previous is not None:
        cmd.append(f"FUZZ_DIRECTIVES={previous.directives}")
    cmd.append("coverage-report")
    return cmd


def feedback_command(
    paths: chain.RoundPaths,
    *,
    previous: chain.RoundPaths | None,
    use_uv: bool,
    llm_feedback: bool,
    llm_model: str | None,
) -> list[str]:
    cmd = python_command(use_uv)
    cmd.extend(
        [
            str(chain.FUZZ_DIR / "coverage_feedback.py"),
            "--target",
            paths.target,
            "--coverage-info",
            str(paths.coverage_info),
            "--coverage-dat",
            str(paths.coverage_dat),
            "--corpus",
            str(paths.corpus),
            "--summary-out",
            str(paths.summary),
            "--directives-out",
            str(paths.directives),
            "--prompt-out",
            str(paths.prompt),
            "--gap-feedback-out",
            str(paths.gap_feedback),
            "--mutation-feedback-out",
            str(paths.mutation_feedback),
            "--llm-response-out",
            str(paths.llm_response),
            "--ignore-functional-coverage",
        ]
    )
    if previous is not None:
        cmd.extend(
            [
                "--previous-summary",
                str(previous.summary),
                "--previous-directives",
                str(previous.directives),
                "--previous-gap-feedback",
                str(previous.gap_feedback),
                "--previous-mutation-feedback",
                str(previous.mutation_feedback),
            ]
        )
    if llm_feedback:
        cmd.append("--llm")
        if llm_model:
            cmd.extend(["--model", llm_model])
    return cmd


def python_command(use_uv: bool) -> list[str]:
    if use_uv:
        return ["uv", "run", "python3"]
    return [sys.executable or "python3"]


def remove_stale_functional_output(paths: chain.RoundPaths) -> None:
    for path in (paths.functional, paths.ignored_functional):
        if path.exists():
            path.unlink()


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        raise SystemExit(f"env file does not exist: {path}")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = strip_env_value(value.strip())


def strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    markdown = chain.render_markdown(comparison)
    return markdown.replace(
        "# Feedback Chain Comparison\n",
        (
            "# Feedback Chain Code-Only Comparison\n\n"
            "- Feedback generation ignored UVM functional coverage.\n"
        ),
        1,
    )


def render_coverage_markdown(report: dict[str, Any]) -> str:
    markdown = chain.render_code_coverage_markdown(report)
    return markdown.replace(
        "# Feedback Chain Code Coverage\n",
        (
            "# Feedback Chain Code-Only Coverage\n\n"
            "- Feedback generation and evaluation both ignore UVM functional coverage.\n"
        ),
        1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
