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
class RoundPaths:
    target: str
    mode: str
    index: int
    run_dir: Path

    @property
    def summary(self) -> Path:
        return self.run_dir / f"{self.target}_coverage_summary.json"

    @property
    def functional(self) -> Path:
        return self.run_dir / f"{self.target}_uvm_functional_coverage.json"

    @property
    def ignored_functional(self) -> Path:
        return self.run_dir / f"{self.target}_ignored_uvm_functional_coverage.json"

    @property
    def coverage_info(self) -> Path:
        return self.run_dir / f"{self.target}_coverage.info"

    @property
    def coverage_dat(self) -> Path:
        return self.run_dir / f"{self.target}_coverage.dat"

    @property
    def prompt(self) -> Path:
        return self.run_dir / f"{self.target}_llm_prompt.json"

    @property
    def llm_response(self) -> Path:
        return self.run_dir / f"{self.target}_llm_response.json"

    @property
    def directives(self) -> Path:
        return self.run_dir / f"{self.target}_mutation_directives.json"

    @property
    def gap_feedback(self) -> Path:
        return self.run_dir / f"{self.target}_gap_feedback.json"

    @property
    def mutation_feedback(self) -> Path:
        return self.run_dir / f"{self.target}_mutation_feedback.json"

    @property
    def corpus(self) -> Path:
        return self.run_dir / f"{self.target}_{self.mode}_round_{self.index:02d}_corpus.jsonl"

    @property
    def log(self) -> Path:
        return self.run_dir / "coverage_feedback.log"


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

MODE_ORDER = ("no_feedback", "heuristic_feedback", "llm_feedback")
FEEDBACK_MODES = {"heuristic_feedback", "llm_feedback"}
COVERAGE_KINDS = ("overall", "line", "toggle", "branch", "expression")
CODE_GAIN_EPSILON = 1e-9


def main() -> int:
    args = parse_args()
    targets = select_targets(args.target)
    modes = normalize_modes(args.modes)
    out_dir = args.out_dir.resolve()

    if args.clean and out_dir.exists() and not args.reuse_existing:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    use_uv = resolve_uv_mode(args.use_uv)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

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
        comparisons.append(build_target_comparison(spec.target, modes, args.rounds, target_root))

    comparison = {
        "schema_version": 1,
        "rounds": args.rounds,
        "modes": list(modes),
        "targets": comparisons,
    }
    json_out = out_dir / "feedback_chain_comparison.json"
    markdown_out = out_dir / "feedback_chain_comparison.md"
    write_json(json_out, comparison)
    markdown = render_markdown(comparison)
    markdown_out.write_text(markdown)

    code_report = build_code_coverage_report(comparison)
    code_json_out = out_dir / "feedback_chain_code_coverage.json"
    code_markdown_out = out_dir / "feedback_chain_code_coverage.md"
    write_json(code_json_out, code_report)
    code_markdown_out.write_text(render_code_coverage_markdown(code_report))

    print(markdown)
    print(f"Wrote {json_out}")
    print(f"Wrote {markdown_out}")
    print(f"Wrote {code_json_out}")
    print(f"Wrote {code_markdown_out}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run and compare full multi-round fuzz campaigns with no feedback, "
            "heuristic feedback, and LLM feedback."
        )
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
        default="no_feedback,heuristic_feedback,llm_feedback",
        help="Comma-separated modes to compare.",
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=FUZZ_DIR / "coverage" / "feedback_chain_compare",
    )
    parser.add_argument("--libafl-iters", type=int, default=256)
    parser.add_argument("--libafl-max-seeds", type=int, default=32)
    parser.add_argument("--libafl-seed", type=int, default=1)
    parser.add_argument(
        "--use-uv",
        choices=["auto", "always", "never"],
        default="auto",
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


def select_targets(raw_targets: list[str] | None) -> list[TargetSpec]:
    if not raw_targets or "all" in raw_targets:
        names = list(TARGETS)
    else:
        names = raw_targets
    return [TARGETS[name] for name in names]


def normalize_modes(raw_modes: str) -> tuple[str, ...]:
    requested = tuple(mode.strip() for mode in raw_modes.split(",") if mode.strip())
    unknown = sorted(set(requested).difference(MODE_ORDER))
    if unknown:
        raise SystemExit(f"unknown mode(s): {', '.join(unknown)}")
    if not requested:
        raise SystemExit("provide at least one mode")
    return tuple(mode for mode in MODE_ORDER if mode in requested)


def resolve_uv_mode(mode: str) -> bool:
    if mode == "always":
        if shutil.which("uv") is None:
            raise SystemExit("uv was requested but was not found on PATH")
        return True
    if mode == "never":
        return False
    return shutil.which("uv") is not None


def run_target_campaign(
    spec: TargetSpec,
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
        previous: RoundPaths | None = None
        for index in range(rounds):
            paths = round_paths(spec.target, mode, index, target_root)
            print(f"\n== {spec.target}: {mode} round {index:02d} ==")
            run_round(
                spec,
                paths,
                previous=previous if mode in FEEDBACK_MODES else None,
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
                source = directive_source(paths.directives)
                if not is_real_llm_source(source):
                    raise SystemExit(
                        f"{spec.target} {mode} round {index:02d}: expected real LLM "
                        f"directives, got {source!r}"
                    )
            previous = paths


def run_round(
    spec: TargetSpec,
    paths: RoundPaths,
    *,
    previous: RoundPaths | None,
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
    if previous is not None:
        cmd.extend(
            [
                f"FUZZ_DIRECTIVES={previous.directives}",
                f"PREVIOUS_COVERAGE_SUMMARY={previous.summary}",
                f"PREVIOUS_MUTATION_DIRECTIVES={previous.directives}",
                f"PREVIOUS_GAP_FEEDBACK={previous.gap_feedback}",
                f"PREVIOUS_MUTATION_FEEDBACK={previous.mutation_feedback}",
            ]
        )
    if llm_feedback:
        cmd.append("LLM_FEEDBACK=1")
        if llm_model:
            cmd.append(f"LLM_MODEL={llm_model}")
    cmd.append("coverage-feedback")
    run_logged(cmd, REPO_ROOT, paths.log, env, quiet=quiet)


def build_target_comparison(
    target: str,
    modes: tuple[str, ...],
    rounds: int,
    target_root: Path,
    *,
    ignore_functional: bool = False,
) -> dict[str, Any]:
    mode_rows = []
    for mode in modes:
        round_rows = []
        for index in range(rounds):
            paths = round_paths(target, mode, index, target_root)
            if not paths.summary.exists():
                raise SystemExit(f"missing round summary: {paths.summary}")
            round_rows.append(summarize_round(paths, ignore_functional=ignore_functional))
        mode_rows.append(summarize_mode(mode, round_rows))
    return {
        "target": target,
        "modes": mode_rows,
        "deltas": mode_deltas(mode_rows),
    }


def summarize_round(paths: RoundPaths, *, ignore_functional: bool = False) -> dict[str, Any]:
    summary = load_json(paths.summary)
    functional = (
        {}
        if ignore_functional
        else load_json(paths.functional) if paths.functional.exists() else {}
    )
    gap_feedback = load_json(paths.gap_feedback) if paths.gap_feedback.exists() else {}
    mutation_feedback = (
        load_json(paths.mutation_feedback) if paths.mutation_feedback.exists() else {}
    )
    structure = summary.get("rtl_structure_coverage", {})
    by_kind = structure.get("by_kind", {}) if isinstance(structure, dict) else {}
    return {
        "index": paths.index,
        "run_dir": str(paths.run_dir),
        "cases": total_cases(summary, functional),
        "uncovered_lines": int(summary.get("uncovered_line_count", 0)),
        "functional_uncovered": functional.get("uncovered", {})
        if isinstance(functional, dict)
        else {},
        "open_gap_count": open_gap_count(summary),
        "overall": coverage_entry(structure),
        "line": coverage_entry(by_kind.get("line", {})),
        "toggle": coverage_entry(by_kind.get("toggle", {})),
        "branch": coverage_entry(by_kind.get("branch", {})),
        "expression": coverage_entry(by_kind.get("expression", {})),
        "directives_source": directive_source(paths.directives),
        "llm_real": is_real_llm_source(directive_source(paths.directives)),
        "layer2": feedback_counts(gap_feedback),
        "layer3": mutation_counts(mutation_feedback),
    }


def summarize_mode(mode: str, rounds: list[dict[str, Any]]) -> dict[str, Any]:
    final = rounds[-1]
    coverage_values = [
        coverage_value(row.get("overall", {}))
        for row in rounds
        if coverage_value(row.get("overall", {})) is not None
    ]
    return {
        "mode": mode,
        "rounds": rounds,
        "final": final,
        "best_overall": max(coverage_values) if coverage_values else None,
        "coverage_auc_avg": round(sum(coverage_values) / len(coverage_values), 6)
        if coverage_values
        else None,
        "code_coverage": summarize_code_coverage(rounds),
        "aggregate_feedback": aggregate_feedback(rounds),
    }


def summarize_code_coverage(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    final = rounds[-1]
    first = rounds[0]
    return {
        "rounds": [code_coverage_snapshot(row) for row in rounds],
        "final": code_coverage_snapshot(final),
        "best": {
            kind: best_coverage_value(rounds, kind)
            for kind in COVERAGE_KINDS
        },
        "auc_avg": {
            kind: average_coverage_value(rounds, kind)
            for kind in COVERAGE_KINDS
        },
        "gain_from_first": code_coverage_delta(final, first),
    }


def code_coverage_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": row.get("index"),
        "run_dir": row.get("run_dir"),
        "cases": row.get("cases", 0),
        "uncovered_lines": row.get("uncovered_lines", 0),
        **{kind: row.get(kind, {}) for kind in COVERAGE_KINDS},
    }


def best_coverage_value(rounds: list[dict[str, Any]], kind: str) -> float | None:
    values = [
        coverage_value(row.get(kind, {}))
        for row in rounds
        if coverage_value(row.get(kind, {})) is not None
    ]
    return max(values) if values else None


def average_coverage_value(rounds: list[dict[str, Any]], kind: str) -> float | None:
    values = [
        coverage_value(row.get(kind, {}))
        for row in rounds
        if coverage_value(row.get(kind, {})) is not None
    ]
    return round(sum(values) / len(values), 6) if values else None


def code_coverage_delta(
    current: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    current_overall = current.get("overall", {})
    reference_overall = reference.get("overall", {})
    return {
        **{
            kind: nullable_delta(
                coverage_value(current.get(kind, {})),
                coverage_value(reference.get(kind, {})),
            )
            for kind in COVERAGE_KINDS
        },
        "uncovered_lines": nullable_int_delta(
            current.get("uncovered_lines"),
            reference.get("uncovered_lines"),
        ),
        "cases": nullable_int_delta(current.get("cases"), reference.get("cases")),
        "hit_delta": nullable_int_delta(
            current_overall.get("hit") if isinstance(current_overall, dict) else None,
            reference_overall.get("hit") if isinstance(reference_overall, dict) else None,
        ),
        "coverage_point_uncovered_delta": nullable_int_delta(
            current_overall.get("uncovered") if isinstance(current_overall, dict) else None,
            reference_overall.get("uncovered") if isinstance(reference_overall, dict) else None,
        ),
    }


def nullable_int_delta(current: Any, reference: Any) -> int | None:
    if current is None or reference is None:
        return None
    try:
        return int(current) - int(reference)
    except (TypeError, ValueError):
        return None


def build_code_coverage_report(comparison: dict[str, Any]) -> dict[str, Any]:
    targets = []
    for target in comparison.get("targets", []):
        modes = target.get("modes", [])
        code_deltas = code_coverage_deltas(modes)
        targets.append(
            {
                "target": target.get("target", ""),
                "modes": [
                    {
                        "mode": mode.get("mode", ""),
                        "code_coverage": mode.get("code_coverage", {}),
                    }
                    for mode in modes
                ],
                "deltas": code_deltas,
                "defect_signals": code_coverage_defect_signals(modes, code_deltas),
            }
        )
    return {
        "schema_version": 1,
        "scope": "rtl_code_coverage_only",
        "rounds": comparison.get("rounds", 0),
        "modes": comparison.get("modes", []),
        "targets": targets,
    }


def code_coverage_deltas(modes: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {mode.get("mode", ""): mode for mode in modes}
    baseline = by_name.get("no_feedback")
    heuristic = by_name.get("heuristic_feedback")
    result: dict[str, Any] = {}
    for mode in modes:
        name = mode.get("mode", "")
        current_final = mode.get("final", {})
        values: dict[str, Any] = {}
        if baseline is not None and name != "no_feedback":
            values["vs_no_feedback"] = code_coverage_delta(
                current_final,
                baseline.get("final", {}),
            )
            values["auc_vs_no_feedback"] = coverage_map_delta(
                mode.get("code_coverage", {}).get("auc_avg", {}),
                baseline.get("code_coverage", {}).get("auc_avg", {}),
            )
        if heuristic is not None and name == "llm_feedback":
            values["vs_heuristic_feedback"] = code_coverage_delta(
                current_final,
                heuristic.get("final", {}),
            )
            values["auc_vs_heuristic_feedback"] = coverage_map_delta(
                mode.get("code_coverage", {}).get("auc_avg", {}),
                heuristic.get("code_coverage", {}).get("auc_avg", {}),
            )
        result[name] = values
    return result


def coverage_map_delta(current: Any, reference: Any) -> dict[str, float | None]:
    current_map = current if isinstance(current, dict) else {}
    reference_map = reference if isinstance(reference, dict) else {}
    return {
        kind: nullable_delta(current_map.get(kind), reference_map.get(kind))
        for kind in COVERAGE_KINDS
    }


def code_coverage_defect_signals(
    modes: list[dict[str, Any]],
    deltas: dict[str, Any],
) -> list[dict[str, str]]:
    by_name = {mode.get("mode", ""): mode for mode in modes}
    signals: list[dict[str, str]] = []
    heuristic_delta = deltas.get("heuristic_feedback", {}).get("vs_no_feedback")
    if isinstance(heuristic_delta, dict):
        add_code_gain_signal(
            signals,
            "heuristic_feedback",
            "no_gain_vs_no_feedback",
            heuristic_delta,
            "heuristic feedback did not improve final RTL code coverage over no_feedback",
        )
    llm_delta = deltas.get("llm_feedback", {}).get("vs_no_feedback")
    if isinstance(llm_delta, dict):
        add_code_gain_signal(
            signals,
            "llm_feedback",
            "no_gain_vs_no_feedback",
            llm_delta,
            "LLM feedback did not improve final RTL code coverage over no_feedback",
        )
    llm_vs_heuristic = deltas.get("llm_feedback", {}).get("vs_heuristic_feedback")
    if isinstance(llm_vs_heuristic, dict):
        add_code_gain_signal(
            signals,
            "llm_feedback",
            "no_gain_vs_heuristic_feedback",
            llm_vs_heuristic,
            "LLM feedback did not improve final RTL code coverage over heuristic_feedback",
        )
    for name, mode in by_name.items():
        gain = mode.get("code_coverage", {}).get("gain_from_first", {})
        if isinstance(gain, dict):
            overall = numeric_or_none(gain.get("overall"))
            line = numeric_or_none(gain.get("line"))
            cases = numeric_or_none(gain.get("cases"))
            if (overall is None or overall <= CODE_GAIN_EPSILON) and cases and cases > 0:
                signals.append(
                    {
                        "mode": name,
                        "signal": "more_cases_no_code_gain",
                        "detail": "rounds added test cases without increasing RTL code coverage",
                    }
                )
            if overall and overall > CODE_GAIN_EPSILON and (
                line is None or line <= CODE_GAIN_EPSILON
            ):
                signals.append(
                    {
                        "mode": name,
                        "signal": "overall_gain_line_stagnant",
                        "detail": "overall RTL coverage improved, but line coverage did not",
                    }
                )
    return signals


def add_code_gain_signal(
    signals: list[dict[str, str]],
    mode: str,
    signal: str,
    delta: dict[str, Any],
    detail: str,
) -> None:
    overall = numeric_or_none(delta.get("overall"))
    if overall is None or overall <= CODE_GAIN_EPSILON:
        signals.append({"mode": mode, "signal": signal, "detail": detail})


def numeric_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mode_deltas(modes: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {mode["mode"]: mode for mode in modes}
    baseline = by_name.get("no_feedback")
    heuristic = by_name.get("heuristic_feedback")
    result: dict[str, Any] = {}
    for mode in modes:
        name = mode["mode"]
        final_cov = coverage_value(mode["final"].get("overall", {}))
        values: dict[str, Any] = {}
        if baseline is not None:
            values["overall_vs_no_feedback"] = nullable_delta(
                final_cov,
                coverage_value(baseline["final"].get("overall", {})),
            )
            values["uncovered_lines_vs_no_feedback"] = (
                int(mode["final"].get("uncovered_lines", 0))
                - int(baseline["final"].get("uncovered_lines", 0))
            )
        if heuristic is not None and name != "heuristic_feedback":
            values["overall_vs_heuristic_feedback"] = nullable_delta(
                final_cov,
                coverage_value(heuristic["final"].get("overall", {})),
            )
        result[name] = values
    return result


def aggregate_feedback(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    layer2_status: dict[str, int] = {}
    layer2_actions: dict[str, int] = {}
    layer3_decisions: dict[str, int] = {}
    suppressed = 0
    for row in rounds:
        merge_counts(layer2_status, row.get("layer2", {}).get("status_counts", {}))
        merge_counts(layer2_actions, row.get("layer2", {}).get("next_action_counts", {}))
        merge_counts(layer3_decisions, row.get("layer3", {}).get("decision_counts", {}))
        suppressed += int(row.get("layer3", {}).get("suppressed_direction_count", 0))
    return {
        "layer2_status_counts": dict(sorted(layer2_status.items())),
        "layer2_next_action_counts": dict(sorted(layer2_actions.items())),
        "layer3_decision_counts": dict(sorted(layer3_decisions.items())),
        "suppressed_direction_count": suppressed,
    }


def feedback_counts(feedback: dict[str, Any]) -> dict[str, Any]:
    return {
        "status_counts": feedback.get("status_counts", {}) if isinstance(feedback, dict) else {},
        "next_action_counts": feedback.get("next_action_counts", {})
        if isinstance(feedback, dict)
        else {},
    }


def mutation_counts(feedback: dict[str, Any]) -> dict[str, Any]:
    directions = feedback.get("directions", {}) if isinstance(feedback, dict) else {}
    decisions: dict[str, int] = {}
    suppressed = 0
    best_name = None
    best_score = None
    if isinstance(directions, dict):
        for name, direction in directions.items():
            if not isinstance(direction, dict):
                continue
            decision = str(direction.get("decision", "unknown"))
            decisions[decision] = decisions.get(decision, 0) + 1
            if decision == "suppress_temporarily":
                suppressed += 1
            try:
                score = float(direction.get("score", 0.0))
            except (TypeError, ValueError):
                score = 0.0
            if best_score is None or score > best_score:
                best_score = score
                best_name = str(name)
    return {
        "decision_counts": dict(sorted(decisions.items())),
        "suppressed_direction_count": suppressed,
        "best_direction": best_name,
        "aggregate_delta": feedback.get("aggregate_delta", {}) if isinstance(feedback, dict) else {},
    }


def total_cases(summary: dict[str, Any], functional: Any) -> int:
    if isinstance(functional, dict):
        try:
            return int(functional.get("total_cases"))
        except (TypeError, ValueError):
            pass
    try:
        return int(summary.get("stimulus_summary", {}).get("total_cases"))
    except (TypeError, ValueError):
        return 0


def coverage_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    totals = value.get("totals")
    if isinstance(totals, dict):
        return totals
    return value


def coverage_value(value: Any) -> float | None:
    if not isinstance(value, dict) or value.get("coverage") is None:
        return None
    try:
        return float(value["coverage"])
    except (TypeError, ValueError):
        return None


def nullable_delta(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None:
        return None
    return round(current - baseline, 6)


def open_gap_count(summary: dict[str, Any]) -> int:
    gaps = summary.get("rtl_gap_summary", {}).get("top_gaps", [])
    return len(gaps) if isinstance(gaps, list) else 0


def merge_counts(target: dict[str, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        try:
            target[str(key)] = target.get(str(key), 0) + int(value)
        except (TypeError, ValueError):
            continue


def round_paths(target: str, mode: str, index: int, target_root: Path) -> RoundPaths:
    return RoundPaths(target, mode, index, target_root / mode / f"round_{index:02d}")


def directive_source(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        return str(load_json(path).get("source", "unknown"))
    except (json.JSONDecodeError, SystemExit):
        return "invalid_json"


def is_real_llm_source(source: str) -> bool:
    lowered = source.lower()
    return "llm" in lowered and "heuristic" not in lowered and "failed" not in lowered


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


def make_command(use_uv: bool) -> list[str]:
    return ["uv", "run", "make"] if use_uv else ["make"]


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
    quiet: bool,
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


def render_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "# Feedback Chain Comparison",
        "",
        f"- Rounds: `{comparison.get('rounds', 0)}`",
        f"- Modes: `{', '.join(comparison.get('modes', []))}`",
        "",
        "## Final Coverage",
        "",
        "| Target | Mode | Final Overall | Best Overall | AUC Avg | Cases | Uncovered Lines | Functional Gap | LLM Real |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for target in comparison.get("targets", []):
        for mode in target.get("modes", []):
            final = mode.get("final", {})
            lines.append(
                "| {target} | {mode} | {overall} | {best} | {auc} | {cases} | "
                "{lines} | {functional} | {llm} |".format(
                    target=target.get("target", ""),
                    mode=mode.get("mode", ""),
                    overall=format_coverage(final.get("overall", {})),
                    best=format_percent(mode.get("best_overall")),
                    auc=format_percent(mode.get("coverage_auc_avg")),
                    cases=final.get("cases", 0),
                    lines=final.get("uncovered_lines", 0),
                    functional=escape_table(format_functional_gap(final.get("functional_uncovered", {}))),
                    llm=final.get("llm_real", False),
                )
            )

    lines.extend(
        [
            "",
            "## Delta",
            "",
            "| Target | Mode | Overall vs No Feedback | Lines vs No Feedback | Overall vs Heuristic |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for target in comparison.get("targets", []):
        deltas = target.get("deltas", {})
        for mode in target.get("modes", []):
            values = deltas.get(mode.get("mode", ""), {})
            lines.append(
                "| {target} | {mode} | {no_fb} | {lines_delta} | {heuristic} |".format(
                    target=target.get("target", ""),
                    mode=mode.get("mode", ""),
                    no_fb=format_delta(values.get("overall_vs_no_feedback")),
                    lines_delta=format_signed_int(values.get("uncovered_lines_vs_no_feedback")),
                    heuristic=format_delta(values.get("overall_vs_heuristic_feedback")),
                )
            )

    lines.extend(
        [
            "",
            "## Feedback Activity",
            "",
            "| Target | Mode | Layer2 Status | Layer2 Actions | Layer3 Decisions | Suppressed |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for target in comparison.get("targets", []):
        for mode in target.get("modes", []):
            feedback = mode.get("aggregate_feedback", {})
            lines.append(
                "| {target} | {mode} | {l2_status} | {l2_actions} | {l3} | {suppressed} |".format(
                    target=target.get("target", ""),
                    mode=mode.get("mode", ""),
                    l2_status=escape_table(format_counts(feedback.get("layer2_status_counts", {}))),
                    l2_actions=escape_table(format_counts(feedback.get("layer2_next_action_counts", {}))),
                    l3=escape_table(format_counts(feedback.get("layer3_decision_counts", {}))),
                    suppressed=feedback.get("suppressed_direction_count", 0),
                )
            )

    notes = comparison_notes(comparison)
    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def render_code_coverage_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Feedback Chain Code Coverage",
        "",
        f"- Scope: `{report.get('scope', 'rtl_code_coverage_only')}`",
        "- Functional coverage and functional gaps are intentionally omitted from this report.",
        f"- Rounds: `{report.get('rounds', 0)}`",
        f"- Modes: `{', '.join(report.get('modes', []))}`",
        "",
        "## Final RTL Coverage",
        "",
        "| Target | Mode | Overall | Line | Toggle | Branch | Expr | Uncovered Lines | Hits | Uncovered Points | Best Overall | AUC Overall |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for target in report.get("targets", []):
        for mode in target.get("modes", []):
            coverage = mode.get("code_coverage", {})
            final = coverage.get("final", {})
            overall = final.get("overall", {}) if isinstance(final, dict) else {}
            best = coverage.get("best", {})
            auc = coverage.get("auc_avg", {})
            lines.append(
                "| {target} | {mode} | {overall} | {line} | {toggle} | {branch} | "
                "{expr} | {uncovered_lines} | {hits} | {uncovered_points} | {best} | {auc} |".format(
                    target=target.get("target", ""),
                    mode=mode.get("mode", ""),
                    overall=format_coverage(final.get("overall", {})),
                    line=format_coverage(final.get("line", {})),
                    toggle=format_coverage(final.get("toggle", {})),
                    branch=format_coverage(final.get("branch", {})),
                    expr=format_coverage(final.get("expression", {})),
                    uncovered_lines=final.get("uncovered_lines", 0),
                    hits=format_plain_int(
                        overall.get("hit") if isinstance(overall, dict) else None
                    ),
                    uncovered_points=format_plain_int(
                        overall.get("uncovered") if isinstance(overall, dict) else None
                    ),
                    best=format_percent(
                        best.get("overall") if isinstance(best, dict) else None
                    ),
                    auc=format_percent(auc.get("overall") if isinstance(auc, dict) else None),
                )
            )

    lines.extend(
        [
            "",
            "## Gain From First Round",
            "",
            "| Target | Mode | Overall Δ | Line Δ | Toggle Δ | Branch Δ | Expr Δ | Uncovered Lines Δ | Hit Δ | Uncovered Points Δ | Cases Δ |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for target in report.get("targets", []):
        for mode in target.get("modes", []):
            gain = mode.get("code_coverage", {}).get("gain_from_first", {})
            lines.append(
                "| {target} | {mode} | {overall} | {line} | {toggle} | {branch} | "
                "{expr} | {uncovered_lines} | {hits} | {points} | {cases} |".format(
                    target=target.get("target", ""),
                    mode=mode.get("mode", ""),
                    overall=format_delta(gain.get("overall")),
                    line=format_delta(gain.get("line")),
                    toggle=format_delta(gain.get("toggle")),
                    branch=format_delta(gain.get("branch")),
                    expr=format_delta(gain.get("expression")),
                    uncovered_lines=format_signed_int(gain.get("uncovered_lines")),
                    hits=format_signed_int(gain.get("hit_delta")),
                    points=format_signed_int(gain.get("coverage_point_uncovered_delta")),
                    cases=format_signed_int(gain.get("cases")),
                )
            )

    lines.extend(
        [
            "",
            "## Gain vs Reference",
            "",
            "| Target | Mode | Reference | Overall Δ | Line Δ | Toggle Δ | Branch Δ | Expr Δ | Uncovered Lines Δ | Hit Δ | Uncovered Points Δ | AUC Overall Δ | Cases Δ |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for target in report.get("targets", []):
        deltas = target.get("deltas", {})
        for mode in target.get("modes", []):
            mode_name = mode.get("mode", "")
            mode_deltas = deltas.get(mode_name, {})
            for reference_key, reference_label in (
                ("vs_no_feedback", "no_feedback"),
                ("vs_heuristic_feedback", "heuristic_feedback"),
            ):
                delta = mode_deltas.get(reference_key)
                if not isinstance(delta, dict):
                    continue
                auc_key = f"auc_{reference_key}"
                auc_delta = mode_deltas.get(auc_key, {})
                lines.append(
                    "| {target} | {mode} | {reference} | {overall} | {line} | {toggle} | "
                    "{branch} | {expr} | {uncovered_lines} | {hits} | {points} | "
                    "{auc} | {cases} |".format(
                        target=target.get("target", ""),
                        mode=mode_name,
                        reference=reference_label,
                        overall=format_delta(delta.get("overall")),
                        line=format_delta(delta.get("line")),
                        toggle=format_delta(delta.get("toggle")),
                        branch=format_delta(delta.get("branch")),
                        expr=format_delta(delta.get("expression")),
                        uncovered_lines=format_signed_int(delta.get("uncovered_lines")),
                        hits=format_signed_int(delta.get("hit_delta")),
                        points=format_signed_int(delta.get("coverage_point_uncovered_delta")),
                        auc=format_delta(
                            auc_delta.get("overall") if isinstance(auc_delta, dict) else None
                        ),
                        cases=format_signed_int(delta.get("cases")),
                    )
                )

    signals = [
        (target.get("target", ""), signal)
        for target in report.get("targets", [])
        for signal in target.get("defect_signals", [])
    ]
    if signals:
        lines.extend(
            [
                "",
                "## Defect Signals",
                "",
                "| Target | Mode | Signal | Detail |",
                "| --- | --- | --- | --- |",
            ]
        )
        for target_name, signal in signals:
            lines.append(
                "| {target} | {mode} | {signal} | {detail} |".format(
                    target=target_name,
                    mode=signal.get("mode", ""),
                    signal=signal.get("signal", ""),
                    detail=escape_table(signal.get("detail", "")),
                )
            )
    return "\n".join(lines) + "\n"


def comparison_notes(comparison: dict[str, Any]) -> list[str]:
    notes = []
    for target in comparison.get("targets", []):
        for mode in target.get("modes", []):
            if mode.get("mode") == "llm_feedback" and not mode.get("final", {}).get("llm_real"):
                notes.append(
                    f"{target.get('target', '')}: llm_feedback final directives were not produced by a real LLM."
                )
            if mode.get("aggregate_feedback", {}).get("layer2_status_counts", {}) == {}:
                notes.append(
                    f"{target.get('target', '')} {mode.get('mode', '')}: no Layer 2 gap state was available in the summarized rounds."
                )
    return notes


def format_coverage(value: Any) -> str:
    cov = coverage_value(value)
    return format_percent(cov)


def format_percent(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100.0:.3f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_delta(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100.0:+.3f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_signed_int(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{int(value):+d}"
    except (TypeError, ValueError):
        return "n/a"


def format_plain_int(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{int(value)}"
    except (TypeError, ValueError):
        return "n/a"


def format_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(f"{key}={count}" for key, count in sorted(value.items()))


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
