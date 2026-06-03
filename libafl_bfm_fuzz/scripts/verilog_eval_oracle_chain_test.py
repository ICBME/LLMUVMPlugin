#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
PY_DIR = REPO_ROOT / "libafl_bfm_fuzz" / "py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from rtlagent_bfm.codegen.oracle_codegen import (  # noqa: E402
    build_oracle_plugin_bundle,
    write_oracle_plugin_bundle,
)
from rtlagent_bfm.codegen.pipeline import CodegenPipelineConfig, finalize_bundle  # noqa: E402
from rtlagent_bfm.codegen.validation import GoldenCase  # noqa: E402


DEFAULT_DATASET = REPO_ROOT / "example" / "verilog-eval" / "dataset_spec-to-rtl"
DEFAULT_OUT_DIR = REPO_ROOT / "libafl_bfm_fuzz" / "coverage" / "verilog_eval_oracle_chain"
DEFAULT_MAX_INPUT_BITS = 8
DEFAULT_LIMIT = 12
CASE_LINE_RE = re.compile(r"^VECASE\s+(\d+)\s+([0-9a-fA-FxXzZ]+)\s+([0-9a-fA-FxXzZ]+)\s*$")


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    width: int


@dataclass(frozen=True)
class ProblemSpec:
    problem: str
    ref_path: Path
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]
    source: str

    @property
    def target(self) -> str:
        return f"ve_{safe_name(self.problem)}"

    @property
    def input_bits(self) -> int:
        return sum(port.width for port in self.inputs)

    @property
    def output_bits(self) -> int:
        return sum(port.width for port in self.outputs)


@dataclass(frozen=True)
class GoldenVector:
    data: dict[str, int]
    expected: str
    line_no: int


@dataclass(frozen=True)
class ChainResult:
    problem: str
    target: str
    status: str
    reason: str = ""
    input_bits: int = 0
    output_bits: int = 0
    golden_cases: int = 0
    ref_model: str = ""
    artifact_dir: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "problem": self.problem,
            "target": self.target,
            "status": self.status,
            "reason": self.reason,
            "input_bits": self.input_bits,
            "output_bits": self.output_bits,
            "golden_cases": self.golden_cases,
            "ref_model": self.ref_model,
            "artifact_dir": self.artifact_dir,
        }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verilator = resolve_verilator(args.verilator)
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    specs = load_problem_specs(args.dataset)
    if args.problem:
        requested = set(args.problem)
        specs = [spec for spec in specs if spec.problem in requested]
        missing = sorted(requested.difference(spec.problem for spec in specs))
        if missing:
            raise SystemExit(f"unknown problem(s): {', '.join(missing)}")

    screened: list[ChainResult] = []
    selected: list[ProblemSpec] = []
    for spec in specs:
        reason = screen_reason(spec, max_input_bits=args.max_input_bits)
        if reason:
            screened.append(
                ChainResult(
                    problem=spec.problem,
                    target=spec.target,
                    status="skipped",
                    reason=reason,
                    input_bits=spec.input_bits,
                    output_bits=spec.output_bits,
                )
            )
            continue
        selected.append(spec)

    selected = selected[: max(0, args.limit)] if args.limit is not None else selected
    results: list[ChainResult] = []
    for spec in selected:
        try:
            results.append(
                run_problem_chain(
                    spec,
                    out_dir=out_dir,
                    verilator=verilator,
                    keep_build=args.keep_build,
                    allow_latch=args.allow_latch,
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep batch reports useful
            results.append(
                ChainResult(
                    problem=spec.problem,
                    target=spec.target,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}",
                    input_bits=spec.input_bits,
                    output_bits=spec.output_bits,
                )
            )

    untested_selected = selected[len(results) :]
    for spec in untested_selected:
        results.append(
            ChainResult(
                problem=spec.problem,
                target=spec.target,
                status="skipped",
                reason="not reached",
                input_bits=spec.input_bits,
                output_bits=spec.output_bits,
            )
        )

    report = build_report(
        dataset=args.dataset,
        out_dir=out_dir,
        max_input_bits=args.max_input_bits,
        limit=args.limit,
        screened=screened,
        results=results,
        total_problem_count=len(specs),
    )
    report_path = args.report_out or out_dir / "oracle_chain_report.json"
    markdown_path = args.markdown_out or out_dir / "oracle_chain_report.md"
    write_json(report_path, report)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    print(render_summary(report))
    print(f"Wrote {report_path}")
    print(f"Wrote {markdown_path}")
    if args.fail_on_failure and any(item.status == "failed" for item in results):
        return 1
    if args.fail_on_empty and not any(item.status == "passed" for item in results):
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Screen VerilogEval RefModule problems that can be represented as "
            "finite OracleIR truth-table rules, then validate the existing "
            "OracleIR -> generated ref model -> golden case pipeline."
        )
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--report-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--max-input-bits", type=int, default=DEFAULT_MAX_INPUT_BITS)
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum number of screened-in problems to run. Use --limit -1 for all.",
    )
    parser.add_argument("--problem", action="append", default=[])
    parser.add_argument("--verilator", default="verilator")
    parser.add_argument("--allow-latch", action="store_true")
    parser.add_argument("--keep-build", action="store_true")
    parser.add_argument("--fail-on-failure", action="store_true")
    parser.add_argument("--fail-on-empty", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        args.limit = None
    return args


def resolve_verilator(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(
            f"verilator not found: {name!r}. Install Verilator or pass --verilator /path/to/verilator."
        )
    return path


def load_problem_specs(dataset: Path) -> list[ProblemSpec]:
    if not dataset.exists():
        raise SystemExit(f"missing VerilogEval dataset directory: {dataset}")
    specs = []
    for ref_path in sorted(dataset.glob("*_ref.sv")):
        specs.append(parse_ref_module(ref_path))
    return specs


def parse_ref_module(path: Path) -> ProblemSpec:
    source = path.read_text(encoding="utf-8", errors="replace")
    header_match = re.search(r"module\s+RefModule\s*\((.*?)\)\s*;", source, re.S)
    if not header_match:
        raise ValueError(f"{path}: cannot find RefModule header")
    ports = parse_ports(header_match.group(1))
    problem = path.name.removesuffix("_ref.sv")
    return ProblemSpec(
        problem=problem,
        ref_path=path,
        inputs=tuple(port for port in ports if port.direction == "input"),
        outputs=tuple(port for port in ports if port.direction == "output"),
        source=source,
    )


def parse_ports(header: str) -> list[Port]:
    ports: list[Port] = []
    last_direction: str | None = None
    last_width = 1
    for part in split_top_level_commas(header):
        text = " ".join(part.strip().split())
        if not text:
            continue
        direction, rest = peel_direction(text)
        if direction is None:
            direction = last_direction
            width = last_width
            rest = text
        else:
            width = parse_width(rest)
        if direction is None:
            continue
        name = parse_port_name(rest)
        if not name:
            continue
        ports.append(Port(name=name, direction=direction, width=width))
        last_direction = direction
        last_width = width
    return ports


def split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
        if char in "[({":
            depth += 1
        elif char in "])}":
            depth = max(0, depth - 1)
    if current:
        parts.append("".join(current))
    return parts


def peel_direction(text: str) -> tuple[str | None, str]:
    match = re.match(r"^(input|output)\b(.*)$", text)
    if not match:
        return None, text
    return match.group(1), match.group(2).strip()


def parse_width(text: str) -> int:
    match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", text)
    if not match:
        return 1
    return abs(int(match.group(1)) - int(match.group(2))) + 1


def parse_port_name(text: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\b(reg|logic|wire|signed|integer)\b", " ", text)
    text = text.strip()
    if not text:
        return ""
    return text.split()[-1].strip(" ,;")


def screen_reason(spec: ProblemSpec, *, max_input_bits: int) -> str:
    source = strip_comments(spec.source)
    if not spec.outputs:
        return "no output ports"
    if spec.input_bits > max_input_bits:
        return f"input_bits {spec.input_bits} exceeds max {max_input_bits}"
    if re.search(r"\balways_ff\b", source):
        return "sequential always_ff"
    if re.search(r"@\s*\([^)]*(posedge|negedge)", source):
        return "edge-triggered sequential logic"
    if re.search(r"'\s*[bBoOdDhH][0-9a-fA-F_xXzZ]*[xXzZ]", source):
        return "explicit x/z literal"
    return ""


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def run_problem_chain(
    spec: ProblemSpec,
    *,
    out_dir: Path,
    verilator: str,
    keep_build: bool,
    allow_latch: bool,
) -> ChainResult:
    problem_dir = out_dir / spec.problem
    if problem_dir.exists():
        shutil.rmtree(problem_dir)
    problem_dir.mkdir(parents=True, exist_ok=True)

    vectors = build_golden_vectors(
        spec,
        work_dir=problem_dir / "verilator",
        verilator=verilator,
        keep_build=keep_build,
        allow_latch=allow_latch,
    )
    oracle_ir = build_truth_table_oracle_ir(spec, vectors)
    oracle_ir_path = problem_dir / "oracle_ir.json"
    golden_path = problem_dir / "golden_cases.json"
    bundle_path = problem_dir / "bundle.json"
    write_json(oracle_ir_path, oracle_ir)
    write_json(
        golden_path,
        [
            {
                "target": spec.target,
                "line_no": vector.line_no,
                "data": vector.data,
                "expected": vector.expected,
            }
            for vector in vectors
        ],
    )
    bundle = build_oracle_plugin_bundle(oracle_ir, target=spec.target, package="generated")
    write_oracle_plugin_bundle(bundle_path, bundle)
    finalize_bundle(
        CodegenPipelineConfig(
            bundle_path=bundle_path,
            candidate_dir=problem_dir / "candidate",
            final_dir=problem_dir / "final",
            target=spec.target,
            ref_model=str(bundle.metadata["ref_model"]),
            golden_cases=tuple(
                GoldenCase(
                    target=spec.target,
                    data=vector.data,
                    expected=vector.expected,
                    line_no=vector.line_no,
                )
                for vector in vectors
            ),
        )
    )
    return ChainResult(
        problem=spec.problem,
        target=spec.target,
        status="passed",
        input_bits=spec.input_bits,
        output_bits=spec.output_bits,
        golden_cases=len(vectors),
        ref_model=str(bundle.metadata["ref_model"]),
        artifact_dir=str((problem_dir / "final").resolve()),
    )


def build_golden_vectors(
    spec: ProblemSpec,
    *,
    work_dir: Path,
    verilator: str,
    keep_build: bool,
    allow_latch: bool,
) -> list[GoldenVector]:
    work_dir.mkdir(parents=True, exist_ok=True)
    tb_path = work_dir / "tb.sv"
    tb_path.write_text(render_truth_table_testbench(spec), encoding="utf-8")
    obj_dir = work_dir / "obj"
    compile_log = work_dir / "verilator_compile.log"
    run_log = work_dir / "verilator_run.log"
    command = [
        verilator,
        "--binary",
        "--timing",
        "-Wno-fatal",
        "--top-module",
        "tb",
        "-Mdir",
        str(obj_dir),
        str(spec.ref_path),
        str(tb_path),
    ]
    compile_result = subprocess.run(
        command,
        cwd=work_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    compile_log.write_text(compile_result.stdout, encoding="utf-8")
    if compile_result.returncode != 0:
        raise RuntimeError(f"Verilator compile failed; see {compile_log}")
    if not allow_latch and "%Warning-LATCH" in compile_result.stdout:
        raise RuntimeError(f"Verilator reported latch inference; see {compile_log}")

    executable = obj_dir / "Vtb"
    run_result = subprocess.run(
        [str(executable)],
        cwd=work_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    run_log.write_text(run_result.stdout, encoding="utf-8")
    if run_result.returncode != 0:
        raise RuntimeError(f"Verilator simulation failed; see {run_log}")

    vectors = parse_truth_table_output(spec, run_result.stdout)
    expected_count = 1 << spec.input_bits if spec.input_bits else 1
    if len(vectors) != expected_count:
        raise RuntimeError(f"expected {expected_count} vectors, got {len(vectors)}")
    if not keep_build and obj_dir.exists():
        shutil.rmtree(obj_dir)
    return vectors


def render_truth_table_testbench(spec: ProblemSpec) -> str:
    input_width = max(1, spec.input_bits)
    output_width = max(1, spec.output_bits)
    lines = [
        "module tb;",
        f"  logic [{input_width - 1}:0] input_vec;",
        f"  logic [{output_width - 1}:0] output_vec;",
    ]
    for port in (*spec.inputs, *spec.outputs):
        lines.append(f"  {logic_decl(port)};")
    offset = 0
    for port in spec.inputs:
        lines.append(f"  assign {port.name} = input_vec[{offset} +: {port.width}];")
        offset += port.width
    offset = 0
    for port in spec.outputs:
        lines.append(f"  assign output_vec[{offset} +: {port.width}] = {port.name};")
        offset += port.width
    connections = ", ".join(f".{port.name}({port.name})" for port in (*spec.inputs, *spec.outputs))
    case_count = 1 << spec.input_bits if spec.input_bits else 1
    lines.extend(
        [
            f"  RefModule dut({connections});",
            "  initial begin",
            f"    for (int i = 0; i < {case_count}; i++) begin",
            f"      input_vec = i[{input_width - 1}:0];",
            "      #1;",
            '      $display("VECASE %0d %0h %0h", i, input_vec, output_vec);',
            "    end",
            "    $finish;",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def logic_decl(port: Port) -> str:
    if port.width == 1:
        return f"logic {port.name}"
    return f"logic [{port.width - 1}:0] {port.name}"


def parse_truth_table_output(spec: ProblemSpec, text: str) -> list[GoldenVector]:
    vectors: list[GoldenVector] = []
    input_offsets = field_offsets(spec.inputs)
    output_mask = (1 << spec.output_bits) - 1 if spec.output_bits else 0
    for line in text.splitlines():
        match = CASE_LINE_RE.match(line.strip())
        if not match:
            continue
        line_no = int(match.group(1)) + 1
        input_text = match.group(2).lower()
        output_text = match.group(3).lower()
        if any(char in input_text + output_text for char in "xz"):
            raise RuntimeError(f"truth table contains x/z at vector {line_no}: {line.strip()}")
        input_value = int(input_text, 16)
        output_value = int(output_text, 16) & output_mask
        data = {
            port.name: (input_value >> input_offsets[port.name]) & ((1 << port.width) - 1)
            for port in spec.inputs
        }
        if not data:
            data = {"_dummy": 0}
        vectors.append(
            GoldenVector(
                data=data,
                expected=format(output_value, f"0{spec.output_bits}b"),
                line_no=line_no,
            )
        )
    return vectors


def field_offsets(ports: Iterable[Port]) -> dict[str, int]:
    offsets: dict[str, int] = {}
    offset = 0
    for port in ports:
        offsets[port.name] = offset
        offset += port.width
    return offsets


def build_truth_table_oracle_ir(spec: ProblemSpec, vectors: list[GoldenVector]) -> dict[str, Any]:
    inputs = [
        {
            "name": port.name,
            "type": "int",
            "min": 0,
            "max": (1 << port.width) - 1,
        }
        for port in spec.inputs
    ]
    if not inputs:
        inputs = [{"name": "_dummy", "type": "int", "min": 0, "max": 0}]
    rules = []
    for index, vector in enumerate(vectors):
        rules.append(
            {
                "name": f"case_{index:04d}",
                "when": condition_from_data(vector.data),
                "expected": {"literal": vector.expected},
            }
        )
    return {
        "schema_version": 1,
        "target": spec.target,
        "inputs": inputs,
        "rules": rules,
        "compare": {"kind": "exact", "normalize": []},
        "assumptions": [
            "OracleIR truth-table rules were generated from the VerilogEval RefModule golden simulation.",
            "Expected values are packed in RefModule output port declaration order, least-significant field first.",
        ],
        "metadata": {
            "source": "verilog_eval_oracle_chain_test",
            "problem": spec.problem,
            "ref": str(spec.ref_path),
            "input_bits": spec.input_bits,
            "output_bits": spec.output_bits,
        },
    }


def condition_from_data(data: dict[str, int]) -> Any:
    items = [{"eq": [{"field": name}, value]} for name, value in sorted(data.items())]
    if not items:
        return True
    if len(items) == 1:
        return items[0]
    return {"and": items}


def build_report(
    *,
    dataset: Path,
    out_dir: Path,
    max_input_bits: int,
    limit: int | None,
    screened: list[ChainResult],
    results: list[ChainResult],
    total_problem_count: int,
) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
    screened_in_count = total_problem_count - len(screened)
    return {
        "dataset": str(dataset),
        "out_dir": str(out_dir),
        "max_input_bits": max_input_bits,
        "limit": limit,
        "summary": {
            "total_problem_count": total_problem_count,
            "screened_in_count": screened_in_count,
            "screened_out_count": len(screened),
            "attempted_count": len(results),
            "passed_count": statuses.get("passed", 0),
            "failed_count": statuses.get("failed", 0),
        },
        "results": [result.to_json() for result in results],
        "screened_out": [result.to_json() for result in screened],
    }


def render_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return (
        "VerilogEval OracleIR chain: "
        f"screened_in={summary['screened_in_count']} "
        f"attempted={summary['attempted_count']} "
        f"passed={summary['passed_count']} "
        f"failed={summary['failed_count']}"
    )


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# VerilogEval OracleIR Chain Test",
        "",
        f"- Dataset: `{report['dataset']}`",
        f"- Output: `{report['out_dir']}`",
        f"- Max input bits: `{report['max_input_bits']}`",
        f"- Screened in: `{summary['screened_in_count']}`",
        f"- Attempted: `{summary['attempted_count']}`",
        f"- Passed: `{summary['passed_count']}`",
        f"- Failed: `{summary['failed_count']}`",
        "",
        "## Results",
        "",
        "| Problem | Status | In Bits | Out Bits | Golden Cases | Ref Model | Reason |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["results"]:
        lines.append(
            "| {problem} | {status} | {input_bits} | {output_bits} | {golden_cases} | `{ref_model}` | {reason} |".format(
                problem=item["problem"],
                status=item["status"],
                input_bits=item["input_bits"],
                output_bits=item["output_bits"],
                golden_cases=item["golden_cases"],
                ref_model=item["ref_model"],
                reason=str(item["reason"]).replace("|", "\\|"),
            )
        )
    return "\n".join(lines) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    text = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    return text or "problem"


if __name__ == "__main__":
    raise SystemExit(main())
