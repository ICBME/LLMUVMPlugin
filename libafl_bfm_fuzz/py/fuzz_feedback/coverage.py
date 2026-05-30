from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .rtl_structure_coverage import build_rtl_structure_coverage


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
                source_cache[current_file] = current_file.read_text(errors="replace").splitlines()
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


def parse_corpus(path: Path, target: str) -> dict[str, Any]:
    total = 0
    origins: Counter[str] = Counter()
    summary: dict[str, Any] = {"target": target, "total_cases": 0, "origin_counts": {}}
    target_counts: dict[str, Counter[str]] = {
        "op_counts": Counter(),
        "key_len_counts": Counter(),
        "encdec_counts": Counter(),
        "mode_counts": Counter(),
        "message_length_buckets": Counter(),
    }

    if not path.exists():
        return summary

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        total += 1
        origins[str(data.get("origin", "unknown"))] += 1
        if str(data.get("target", "tinyalu")) != target:
            continue
        if target == "tinyalu":
            target_counts["op_counts"][OP_NAMES.get(int(data["op"]), str(data["op"]))] += 1
        elif target == "aes":
            target_counts["key_len_counts"][str(data["key_len"])] += 1
            target_counts["encdec_counts"][str(data["encdec"])] += 1
        elif target == "sha256":
            target_counts["mode_counts"][str(data["mode"])] += 1
            msg_len = len(bytes.fromhex(str(data["message"])))
            target_counts["message_length_buckets"][length_bucket(msg_len)] += 1

    summary["total_cases"] = total
    summary["origin_counts"] = dict(origins)
    for key, counts in target_counts.items():
        if counts:
            summary[key] = dict(counts)
    return summary


def length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length <= 55:
        return "1..55"
    if length <= 64:
        return "56..64"
    if length <= 127:
        return "65..127"
    return "128+"


def build_summary(
    target: str,
    coverage_info: Path,
    corpus: Path,
    coverage_dat: Path | None = None,
) -> dict[str, Any]:
    uncovered, file_counts = parse_lcov_info(coverage_info)
    return {
        "target": target,
        "coverage_info": str(coverage_info),
        "coverage_dat": str(coverage_dat) if coverage_dat is not None else None,
        "corpus": str(corpus),
        "uncovered_line_count": len(uncovered),
        "uncovered_by_file": file_counts,
        "uncovered_lines": [asdict(line) for line in uncovered[:120]],
        "rtl_structure_coverage": build_rtl_structure_coverage(
            coverage_info=coverage_info,
            coverage_dat=coverage_dat,
        ),
        "stimulus_summary": parse_corpus(corpus, target),
    }
