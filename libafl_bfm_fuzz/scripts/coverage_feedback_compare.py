#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
FUZZ_DIR = REPO_ROOT / "libafl_bfm_fuzz"


@dataclass(frozen=True)
class TargetSpec:
    target: str
    toplevel: str
    rtl_glob: str
    extra_args: str | None = None


@dataclass(frozen=True)
class RunPaths:
    target: str
    mode: str
    run_dir: Path

    @property
    def summary(self) -> Path:
        return self.run_dir / f"{self.target}_coverage_summary.json"

    @property
    def functional(self) -> Path:
        return self.run_dir / f"{self.target}_uvm_functional_coverage.json"

    @property
    def directives(self) -> Path:
        return self.run_dir / f"{self.target}_mutation_directives.json"

    @property
    def corpus(self) -> Path:
        suffix = "corpus" if self.mode == "baseline" else f"{self.mode}_corpus"
        return self.run_dir / f"{self.target}_{suffix}.jsonl"


TARGETS = {
    "secworks_aes": TargetSpec(
        target="secworks_aes",
        toplevel="aes",
        rtl_glob="example/aes/src/rtl/*.v",
        extra_args="-Wno-UNOPTFLAT",
    ),
    "secworks_sha256": TargetSpec(
        target="secworks_sha256",
        toplevel="sha256",
        rtl_glob="example/sha256/src/rtl/*.v",
    ),
}

MODE_ORDER = ("baseline", "heuristic", "llm")


def main() -> int:
    args = parse_args()
    targets = select_targets(args.target)
    modes = normalize_modes(args.modes)
    out_dir = args.out_dir.resolve()

    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    use_uv = resolve_uv_mode(args.use_uv)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    rows: list[dict[str, Any]] = []
    for spec in targets:
        target_root = out_dir / spec.target
        target_root.mkdir(parents=True, exist_ok=True)
        baseline_paths = RunPaths(spec.target, "baseline", target_root / "baseline")
        print(f"\n== {spec.target}: baseline ==")
        run_coverage_feedback(
            spec,
            baseline_paths,
            use_uv=use_uv,
            libafl_iters=args.libafl_iters,
            libafl_max_seeds=args.libafl_max_seeds,
            libafl_seed=args.libafl_seed,
            env=env,
            quiet=args.quiet,
        )
        rows.append(summarize_run(baseline_paths, applied_source="none"))

        if "heuristic" in modes:
            heuristic_paths = RunPaths(spec.target, "heuristic", target_root / "heuristic")
            print(f"\n== {spec.target}: heuristic feedback ==")
            heuristic_source = directive_source(baseline_paths.directives)
            run_coverage_feedback(
                spec,
                heuristic_paths,
                use_uv=use_uv,
                libafl_iters=args.libafl_iters,
                libafl_max_seeds=args.libafl_max_seeds,
                libafl_seed=args.libafl_seed,
                env=env,
                directives=baseline_paths.directives,
                quiet=args.quiet,
            )
            rows.append(summarize_run(heuristic_paths, applied_source=heuristic_source))

        if "llm" in modes:
            llm_paths = RunPaths(spec.target, "llm", target_root / "llm")
            llm_paths.run_dir.mkdir(parents=True, exist_ok=True)
            llm_directives = llm_paths.run_dir / f"{spec.target}_llm_directives.json"
            print(f"\n== {spec.target}: llm feedback directive generation ==")
            generate_llm_directives(
                spec,
                baseline_paths,
                llm_paths.run_dir,
                llm_directives,
                model=args.llm_model,
                env=env,
                quiet=args.quiet,
            )
            llm_source = directive_source(llm_directives)
            if args.require_real_llm and not is_real_llm_source(llm_source):
                raise SystemExit(
                    f"{spec.target}: requested real LLM feedback, but directive source was "
                    f"{llm_source!r}"
                )

            print(f"\n== {spec.target}: llm feedback replay ==")
            run_coverage_feedback(
                spec,
                llm_paths,
                use_uv=use_uv,
                libafl_iters=args.libafl_iters,
                libafl_max_seeds=args.libafl_max_seeds,
                libafl_seed=args.libafl_seed,
                env=env,
                directives=llm_directives,
                quiet=args.quiet,
            )
            rows.append(summarize_run(llm_paths, applied_source=llm_source))

    comparison = build_comparison(rows)
    json_path = out_dir / "coverage_feedback_comparison.json"
    md_path = out_dir / "coverage_feedback_comparison.md"
    json_path.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n")
    markdown = render_markdown(comparison)
    md_path.write_text(markdown)
    print("\n" + markdown)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run baseline, heuristic, and LLM feedback coverage comparisons."
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=["all", *TARGETS.keys()],
        default=None,
        help="Target to run. Repeat for multiple targets. Default: all.",
    )
    parser.add_argument(
        "--modes",
        default="baseline,heuristic,llm",
        help="Comma-separated modes. baseline is always run because feedback modes need it.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=FUZZ_DIR / "coverage" / "feedback_compare",
        help="Output artifact directory.",
    )
    parser.add_argument("--libafl-iters", type=int, default=0)
    parser.add_argument("--libafl-max-seeds", type=int, default=0)
    parser.add_argument("--libafl-seed", type=int, default=1)
    parser.add_argument(
        "--use-uv",
        choices=["auto", "always", "never"],
        default="auto",
        help="Run make through 'uv run' when available.",
    )
    parser.add_argument("--llm-model", default=None)
    parser.add_argument(
        "--require-real-llm",
        action="store_true",
        help="Fail when --llm falls back to heuristic directives.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        help="Keep an existing output directory instead of deleting it first.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Write command output to per-run logs without streaming it to stdout.",
    )
    parser.set_defaults(clean=True)
    return parser.parse_args()


def select_targets(raw_targets: list[str] | None) -> list[TargetSpec]:
    if not raw_targets or "all" in raw_targets:
        names = list(TARGETS)
    else:
        names = raw_targets
    return [TARGETS[name] for name in names]


def normalize_modes(raw_modes: str) -> set[str]:
    requested = {mode.strip() for mode in raw_modes.split(",") if mode.strip()}
    unknown = requested.difference(MODE_ORDER)
    if unknown:
        raise SystemExit(f"unknown mode(s): {', '.join(sorted(unknown))}")
    requested.add("baseline")
    return requested


def resolve_uv_mode(mode: str) -> bool:
    if mode == "always":
        if shutil.which("uv") is None:
            raise SystemExit("uv was requested but was not found on PATH")
        return True
    if mode == "never":
        return False
    return shutil.which("uv") is not None


def run_coverage_feedback(
    spec: TargetSpec,
    paths: RunPaths,
    *,
    use_uv: bool,
    libafl_iters: int,
    libafl_max_seeds: int,
    libafl_seed: int,
    env: dict[str, str],
    directives: Path | None = None,
    quiet: bool = False,
) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    cmd = make_command(use_uv)
    cmd.extend(
        [
            "-C",
            str(FUZZ_DIR),
            f"TARGET={spec.target}",
            f"LIBAFL_ITERS={libafl_iters}",
            f"LIBAFL_MAX_SEEDS={libafl_max_seeds}",
            f"LIBAFL_SEED={libafl_seed}",
            f"COVERAGE_DIR={paths.run_dir}",
            f"FUZZ_CORPUS={paths.corpus}",
            f"VERILOG_SOURCES={rtl_sources(spec)}",
            f"TOPLEVEL={spec.toplevel}",
        ]
    )
    if spec.extra_args:
        cmd.append(f"EXTRA_ARGS={spec.extra_args}")
    if directives is not None:
        cmd.append(f"FUZZ_DIRECTIVES={directives}")
    cmd.append("coverage-feedback")
    run_logged(cmd, REPO_ROOT, paths.run_dir / "coverage_feedback.log", env, quiet=quiet)


def generate_llm_directives(
    spec: TargetSpec,
    baseline: RunPaths,
    out_dir: Path,
    directives_out: Path,
    *,
    model: str | None,
    env: dict[str, str],
    quiet: bool = False,
) -> None:
    cmd = [
        sys.executable,
        str(FUZZ_DIR / "coverage_feedback.py"),
        "--target",
        spec.target,
        "--coverage-info",
        str(baseline.run_dir / f"{spec.target}_coverage.info"),
        "--coverage-dat",
        str(baseline.run_dir / f"{spec.target}_coverage.dat"),
        "--functional-coverage",
        str(baseline.functional),
        "--corpus",
        str(baseline.corpus),
        "--summary-out",
        str(out_dir / f"{spec.target}_llm_input_summary.json"),
        "--directives-out",
        str(directives_out),
        "--prompt-out",
        str(out_dir / f"{spec.target}_llm_prompt.json"),
        "--llm-response-out",
        str(out_dir / f"{spec.target}_llm_response.json"),
        "--llm",
    ]
    if model:
        cmd.extend(["--model", model])
    run_logged(cmd, REPO_ROOT, out_dir / "llm_directives.log", env, quiet=quiet)


def make_command(use_uv: bool) -> list[str]:
    if use_uv:
        return ["uv", "run", "make"]
    return ["make"]


def rtl_sources(spec: TargetSpec) -> str:
    sources = sorted(REPO_ROOT.glob(spec.rtl_glob))
    if not sources:
        raise SystemExit(f"{spec.target}: no RTL sources matched {spec.rtl_glob}")
    return " ".join(str(path.resolve()) for path in sources)


def run_logged(
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    env: dict[str, str],
    *,
    quiet: bool = False,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print("$ " + " ".join(cmd))
    with log_path.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if not quiet:
                print(line, end="")
            log.write(line)
        status = proc.wait()
    if status != 0:
        raise SystemExit(f"command failed with exit code {status}; see {log_path}")


def summarize_run(paths: RunPaths, *, applied_source: str) -> dict[str, Any]:
    summary = json.loads(paths.summary.read_text())
    functional = json.loads(paths.functional.read_text())
    structure = summary["rtl_structure_coverage"]
    by_kind = structure.get("by_kind", {})
    return {
        "target": paths.target,
        "mode": paths.mode,
        "run_dir": str(paths.run_dir),
        "cases": functional.get("total_cases", summary["stimulus_summary"]["total_cases"]),
        "applied_directive_source": applied_source,
        "functional_uncovered": functional.get("uncovered", {}),
        "uncovered_lines": summary.get("uncovered_line_count", 0),
        "overall": structure.get("totals", {}),
        "line": by_kind.get("line", {}),
        "toggle": by_kind.get("toggle", {}),
        "branch": by_kind.get("branch", {}),
        "expression": by_kind.get("expression", {}),
    }


def directive_source(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        return str(json.loads(path.read_text()).get("source", "unknown"))
    except json.JSONDecodeError:
        return "invalid_json"


def is_real_llm_source(source: str) -> bool:
    lowered = source.lower()
    return "llm" in lowered and "heuristic" not in lowered and "failed" not in lowered


def build_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_by_target = {
        row["target"]: row for row in rows if row["mode"] == "baseline"
    }
    enriched = []
    for row in rows:
        baseline = baseline_by_target.get(row["target"], row)
        item = dict(row)
        item["delta_vs_baseline"] = {
            "cases": row["cases"] - baseline["cases"],
            "uncovered_lines": row["uncovered_lines"] - baseline["uncovered_lines"],
            "overall": coverage_delta(row["overall"], baseline["overall"]),
            "line": coverage_delta(row["line"], baseline["line"]),
            "toggle": coverage_delta(row["toggle"], baseline["toggle"]),
            "branch": coverage_delta(row["branch"], baseline["branch"]),
            "expression": coverage_delta(row["expression"], baseline["expression"]),
        }
        enriched.append(item)
    return {
        "summary": enriched,
        "notes": comparison_notes(enriched),
    }


def coverage_delta(current: dict[str, Any], baseline: dict[str, Any]) -> float | None:
    current_cov = current.get("coverage")
    baseline_cov = baseline.get("coverage")
    if current_cov is None or baseline_cov is None:
        return None
    return round(float(current_cov) - float(baseline_cov), 6)


def comparison_notes(rows: list[dict[str, Any]]) -> list[str]:
    notes = []
    llm_rows = [row for row in rows if row["mode"] == "llm"]
    if llm_rows and not any(is_real_llm_source(row["applied_directive_source"]) for row in llm_rows):
        notes.append(
            "LLM mode did not use a real LLM directive source; check OPENAI_API_KEY and llm_response artifacts."
        )
    return notes


def render_markdown(comparison: dict[str, Any]) -> str:
    rows = comparison["summary"]
    lines = [
        "# Coverage Feedback Comparison",
        "",
        "| Target | Mode | Cases | Directive source | Functional gap | Uncovered lines | Overall | Line | Toggle | Branch | Expr |",
        "| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {target} | {mode} | {cases} | {source} | {gap} | {uncovered} | "
            "{overall} | {line} | {toggle} | {branch} | {expr} |".format(
                target=row["target"],
                mode=row["mode"],
                cases=row["cases"],
                source=escape_table(str(row["applied_directive_source"])),
                gap=escape_table(format_functional_gap(row["functional_uncovered"])),
                uncovered=row["uncovered_lines"],
                overall=format_coverage(row["overall"]),
                line=format_coverage(row["line"]),
                toggle=format_coverage(row["toggle"]),
                branch=format_coverage(row["branch"]),
                expr=format_coverage(row["expression"]),
            )
        )
    lines.extend(["", "## Delta vs Baseline", ""])
    lines.append(
        "| Target | Mode | Cases | Uncovered lines | Overall | Line | Toggle | Branch | Expr |"
    )
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        delta = row["delta_vs_baseline"]
        lines.append(
            "| {target} | {mode} | {cases:+d} | {uncovered:+d} | {overall} | {line} | "
            "{toggle} | {branch} | {expr} |".format(
                target=row["target"],
                mode=row["mode"],
                cases=delta["cases"],
                uncovered=delta["uncovered_lines"],
                overall=format_delta(delta["overall"]),
                line=format_delta(delta["line"]),
                toggle=format_delta(delta["toggle"]),
                branch=format_delta(delta["branch"]),
                expr=format_delta(delta["expression"]),
            )
        )
    if comparison.get("notes"):
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in comparison["notes"])
    lines.append("")
    return "\n".join(lines)


def format_coverage(value: dict[str, Any]) -> str:
    coverage = value.get("coverage")
    if coverage is None:
        return "n/a"
    return f"{float(coverage) * 100:.3f}%"


def format_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.3f}%"


def format_functional_gap(value: Any) -> str:
    if not value:
        return "none"
    parts = []
    if isinstance(value, dict):
        for section, gaps in sorted(value.items()):
            if isinstance(gaps, dict):
                for name, missing in sorted(gaps.items()):
                    parts.append(f"{section}.{name}={missing}")
            else:
                parts.append(f"{section}={gaps}")
    else:
        parts.append(str(value))
    return "; ".join(parts)


def escape_table(value: str) -> str:
    return value.replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
