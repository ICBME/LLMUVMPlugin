"""Summarize RTL coverage and emit fuzz mutation directives.

The JSON produced here is deliberately small enough to hand to an LLM. The
script also emits deterministic heuristic directives so the feedback path is
usable without an API key.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Iterable


OP_NAMES = {1: "ADD", 2: "AND", 3: "XOR", 4: "MUL"}


@dataclass(frozen=True)
class UncoveredLine:
    file: str
    line: int
    code: str


def parse_lcov_info(path: Path) -> tuple[list[UncoveredLine], dict[str, int]]:
    uncovered: list[UncoveredLine] = []
    file_counts: Counter[str] = Counter()
    if not path.exists():
        return uncovered, {}

    current_file: Path | None = None
    source_cache: dict[Path, list[str]] = {}
    for raw_line in path.read_text(errors="replace").splitlines():
        if raw_line.startswith("SF:"):
            current_file = Path(raw_line[3:])
            if current_file.exists():
                source_cache[current_file] = current_file.read_text(
                    errors="replace"
                ).splitlines()
            continue
        if current_file is None or not raw_line.startswith("DA:"):
            continue

        line_no_text, count_text = raw_line[3:].split(",", 1)
        line_no = int(line_no_text)
        count = int(count_text)
        if count != 0:
            continue

        source = source_cache.get(current_file, [])
        code = source[line_no - 1].strip() if 0 < line_no <= len(source) else ""
        uncovered.append(UncoveredLine(str(current_file), line_no, code))
        file_counts[str(current_file)] += 1
    return uncovered, dict(file_counts)


def parse_corpus(path: Path) -> dict[str, object]:
    op_counts: Counter[str] = Counter()
    origin_counts: Counter[str] = Counter()
    edge_hits: Counter[str] = Counter()
    total = 0
    if not path.exists():
        return {
            "total_cases": 0,
            "op_counts": {},
            "origin_counts": {},
            "edge_hits": {},
        }

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        total += 1
        a = int(data["a"])
        b = int(data["b"])
        op = int(data["op"])
        op_counts[OP_NAMES.get(op, str(op))] += 1
        origin_counts[str(data.get("origin", "unknown"))] += 1
        for label, value in (("a", a), ("b", b)):
            if value == 0:
                edge_hits[f"{label}_zero"] += 1
            elif value == 0xFF:
                edge_hits[f"{label}_max"] += 1
            elif value in (0x7F, 0x80):
                edge_hits[f"{label}_mid_edge"] += 1

    return {
        "total_cases": total,
        "op_counts": dict(op_counts),
        "origin_counts": dict(origin_counts),
        "edge_hits": dict(edge_hits),
    }


def _contains_any(lines: Iterable[UncoveredLine], patterns: Iterable[str]) -> bool:
    lowered = [pattern.lower() for pattern in patterns]
    for line in lines:
        text = f"{line.file} {line.code}".lower()
        if any(pattern in text for pattern in lowered):
            return True
    return False


def propose_directives(summary: dict[str, object]) -> dict[str, object]:
    uncovered = [UncoveredLine(**line) for line in summary["uncovered_lines"]]
    stimulus = summary["stimulus_summary"]
    op_counts = stimulus.get("op_counts", {})
    directives: list[dict[str, object]] = []

    if _contains_any(uncovered, ("three_cycle", "mult", "done1", "done2", "done3")):
        directives.append(
            {
                "name": "cover_mul_pipeline",
                "reason": "Uncovered RTL is near the three-cycle multiplier path.",
                "ops": ["MUL"],
                "constraints": ["MUL_NONZERO", "MUL_PIPELINE"],
                "operand_pairs": [
                    {"a": "0x01", "b": "0xff"},
                    {"a": "0x7f", "b": "0x80"},
                    {"a": "0xff", "b": "0xfe"},
                ],
                "weight": 4,
            }
        )

    if _contains_any(uncovered, ("3'b001", "add", "+")) or op_counts.get("ADD", 0) < 8:
        directives.append(
            {
                "name": "stress_add_overflow",
                "reason": "ADD line/overflow space needs stronger exercise.",
                "ops": ["ADD"],
                "constraints": ["ADD_OVERFLOW"],
                "operand_pairs": [
                    {"a": "0xff", "b": "0xff"},
                    {"a": "0xff", "b": "0x01"},
                    {"a": "0x80", "b": "0x80"},
                ],
                "weight": 3,
            }
        )

    if _contains_any(uncovered, ("3'b010", "3'b011", "&", "^")):
        directives.append(
            {
                "name": "toggle_bitwise_patterns",
                "reason": "Bitwise operation lines or toggles need dense patterns.",
                "ops": ["AND", "XOR"],
                "constraints": ["BIT_PATTERN"],
                "operand_pairs": [
                    {"a": "0x55", "b": "0xaa"},
                    {"a": "0xaa", "b": "0x55"},
                    {"a": "0x0f", "b": "0xf0"},
                    {"a": "0xf0", "b": "0x0f"},
                ],
                "weight": 3,
            }
        )

    if not directives:
        directives.append(
            {
                "name": "balanced_edge_refresh",
                "reason": "No obvious uncovered operation cluster; refresh edges.",
                "ops": ["ADD", "AND", "XOR", "MUL"],
                "operands": {
                    "A": ["0x00", "0x01", "0x7f", "0x80", "0xfe", "0xff"],
                    "B": ["0x00", "0x01", "0x7f", "0x80", "0xfe", "0xff"],
                },
                "weight": 2,
            }
        )

    return {
        "source": "coverage_feedback.py heuristic; replace or edit with LLM output",
        "directives": directives,
    }


def write_llm_prompt(path: Path, summary: dict[str, object]) -> None:
    prompt = {
        "task": (
            "Analyze RTL coverage gaps and return JSON fuzz mutation directives. "
            "Do not emit executable code."
        ),
        "directive_schema": {
            "directives": [
                {
                    "name": "short_identifier",
                    "reason": "coverage gap being targeted",
                    "ops": ["ADD", "AND", "XOR", "MUL"],
                    "constraints": [
                        "ADD_OVERFLOW",
                        "MUL_NONZERO",
                        "MUL_PIPELINE",
                        "BIT_PATTERN",
                    ],
                    "operand_pairs": [{"a": "0xff", "b": "0x01"}],
                    "weight": 1,
                }
            ]
        },
        "coverage_summary": summary,
    }
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--directives-out", type=Path, required=True)
    parser.add_argument("--prompt-out", type=Path, required=True)
    args = parser.parse_args()

    uncovered, file_counts = parse_lcov_info(args.coverage_info)
    summary = {
        "coverage_info": str(args.coverage_info),
        "corpus": str(args.corpus),
        "uncovered_line_count": len(uncovered),
        "uncovered_by_file": file_counts,
        "uncovered_lines": [asdict(line) for line in uncovered[:80]],
        "stimulus_summary": parse_corpus(args.corpus),
    }

    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    args.directives_out.write_text(
        json.dumps(propose_directives(summary), indent=2, sort_keys=True) + "\n"
    )
    write_llm_prompt(args.prompt_out, summary)


if __name__ == "__main__":
    main()
