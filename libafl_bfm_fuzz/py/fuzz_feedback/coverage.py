from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from .rtl_structure_coverage import build_rtl_structure_coverage
from fuzz_bfm.target_config import FieldSpec, load_target_config
from fuzz_uvm.functional_coverage import build_functional_coverage_from_jsonl


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
    field_counts: dict[str, Counter[str]] = {}
    config = _try_load_target_config(target)
    fields = config.fields if config is not None else ()
    summary: dict[str, Any] = {
        "target": target,
        "total_cases": 0,
        "origin_counts": {},
        "field_counts": {},
    }

    if not path.exists():
        return summary

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        total += 1
        origins[str(data.get("origin", "unknown"))] += 1
        if str(data.get("target", target)) != target:
            continue
        for field in fields:
            if field.name in data:
                field_counts.setdefault(field.name, Counter())[field_summary_value(field, data[field.name])] += 1

    summary["total_cases"] = total
    summary["origin_counts"] = dict(origins)
    summary["field_counts"] = {
        field_name: dict(counts) for field_name, counts in sorted(field_counts.items())
    }
    return summary


def field_summary_value(field: FieldSpec, value: Any) -> str:
    if field.kind == "hex":
        try:
            raw = bytes.fromhex(str(value))
        except ValueError:
            return "invalid_hex"
        return f"{byte_pattern(raw)}:{len(raw)}"
    return str(value)


def byte_pattern(data: bytes) -> str:
    if not data:
        return "empty"
    if all(byte == 0 for byte in data):
        return "zero"
    if all(byte == 0xFF for byte in data):
        return "ff"
    if data == bytes(idx & 0xFF for idx in range(len(data))):
        return "increment"
    return "mixed"


def build_summary(
    target: str,
    coverage_info: Path,
    corpus: Path,
    coverage_dat: Path | None = None,
    functional_coverage: Path | None = None,
) -> dict[str, Any]:
    uncovered, file_counts = parse_lcov_info(coverage_info)
    uvm_functional_coverage, functional_coverage_source = load_functional_coverage(
        target,
        corpus,
        functional_coverage,
    )
    return {
        "target": target,
        "coverage_info": str(coverage_info),
        "coverage_dat": str(coverage_dat) if coverage_dat is not None else None,
        "functional_coverage": (
            str(functional_coverage) if functional_coverage is not None else None
        ),
        "corpus": str(corpus),
        "uncovered_line_count": len(uncovered),
        "uncovered_by_file": file_counts,
        "uncovered_lines": [asdict(line) for line in uncovered[:120]],
        "rtl_structure_coverage": build_rtl_structure_coverage(
            coverage_info=coverage_info,
            coverage_dat=coverage_dat,
        ),
        "uvm_functional_coverage": uvm_functional_coverage,
        "uvm_functional_coverage_source": functional_coverage_source,
        "stimulus_summary": parse_corpus(corpus, target),
    }


def load_functional_coverage(
    target: str,
    corpus: Path,
    functional_coverage: Path | None,
) -> tuple[dict[str, Any], str]:
    if functional_coverage is not None and functional_coverage.exists():
        return json.loads(functional_coverage.read_text()), str(functional_coverage)
    return build_functional_coverage_from_jsonl(corpus, target), "corpus_fallback"


def _try_load_target_config(target: str):
    try:
        return load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
