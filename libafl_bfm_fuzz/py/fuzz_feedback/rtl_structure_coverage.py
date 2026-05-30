from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RTL_STRUCTURAL_COVERAGE_DOMAIN = "rtl_structure"
RTL_STRUCTURAL_COVERAGE_KINDS = ("line", "branch", "expression", "toggle", "fsm", "user")
VERILATOR_KIND_ALIASES = {
    "expr": "expression",
    "line": "line",
    "branch": "branch",
    "toggle": "toggle",
    "fsm": "fsm",
    "user": "user",
}


@dataclass(frozen=True)
class RtlStructuralCoveragePoint:
    """One RTL structural coverage point.

    This model is intentionally separate from future UVM functional coverage.
    It describes tool-instrumented RTL structure such as lines, branches,
    expressions, signal toggles, FSM arcs, and user cover points.
    """

    kind: str
    file: str
    line: int | None
    count: int
    module: str | None = None
    object: str | None = None
    source: str = "verilator"

    @property
    def hit(self) -> bool:
        return self.count > 0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["hit"] = self.hit
        return data


def build_rtl_structure_coverage(
    coverage_info: Path | None = None,
    coverage_dat: Path | None = None,
    max_uncovered_points: int = 160,
) -> dict[str, Any]:
    points: list[RtlStructuralCoveragePoint] = []
    sources: dict[str, str] = {}

    if coverage_info is not None:
        sources["lcov_info"] = str(coverage_info)
        points.extend(parse_lcov_lines(coverage_info))
    if coverage_dat is not None:
        sources["verilator_dat"] = str(coverage_dat)
        dat_points = parse_verilator_dat(coverage_dat)
        if coverage_info is not None:
            dat_points = [point for point in dat_points if point.kind != "line"]
        points.extend(dat_points)

    totals = summarize_points(points)
    return {
        "domain": RTL_STRUCTURAL_COVERAGE_DOMAIN,
        "available_kinds": sorted(totals["by_kind"].keys()),
        "supported_kinds": list(RTL_STRUCTURAL_COVERAGE_KINDS),
        "sources": sources,
        "totals": totals["overall"],
        "by_kind": totals["by_kind"],
        "by_file": totals["by_file"],
        "by_module": totals["by_module"],
        "uncovered_points": [
            point.to_json() for point in sorted_uncovered(points)[:max_uncovered_points]
        ],
    }


def parse_lcov_lines(path: Path) -> list[RtlStructuralCoveragePoint]:
    if not path.exists():
        return []

    points: list[RtlStructuralCoveragePoint] = []
    current_file: str | None = None
    for raw_line in path.read_text(errors="replace").splitlines():
        if raw_line.startswith("SF:"):
            current_file = raw_line[3:]
            continue
        if current_file is None or not raw_line.startswith("DA:"):
            continue

        line_no_text, count_text = raw_line[3:].split(",", 1)
        points.append(
            RtlStructuralCoveragePoint(
                kind="line",
                file=current_file,
                line=int(line_no_text),
                count=int(count_text),
                source="lcov_info",
            )
        )
    return points


def parse_verilator_dat(path: Path) -> list[RtlStructuralCoveragePoint]:
    if not path.exists():
        return []

    points: list[RtlStructuralCoveragePoint] = []
    for line in path.read_text(errors="replace").splitlines():
        point = parse_verilator_dat_line(line)
        if point is not None:
            points.append(point)
    return points


def parse_verilator_dat_line(line: str) -> RtlStructuralCoveragePoint | None:
    if not line.startswith("C '"):
        return None
    try:
        metadata_text, count_text = line[3:].rsplit("' ", 1)
    except ValueError:
        return None
    metadata = parse_verilator_metadata(metadata_text)
    raw_kind = metadata.get("t", "unknown")
    kind = VERILATOR_KIND_ALIASES.get(raw_kind, raw_kind)
    count = int(count_text.strip())
    page = metadata.get("page", "")
    return RtlStructuralCoveragePoint(
        kind=kind,
        file=metadata.get("f", ""),
        line=parse_optional_int(metadata.get("l")),
        count=count,
        module=module_from_page(page) or metadata.get("h"),
        object=metadata.get("o"),
        source="verilator_dat",
    )


def parse_verilator_metadata(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for chunk in text.split("\x01"):
        if "\x02" not in chunk:
            continue
        key, value = chunk.split("\x02", 1)
        metadata[key] = value
    return metadata


def summarize_points(points: list[RtlStructuralCoveragePoint]) -> dict[str, Any]:
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    by_module: dict[str, Counter[str]] = defaultdict(Counter)
    overall: Counter[str] = Counter()

    for point in points:
        add_point(overall, point)
        add_point(by_kind[point.kind], point)
        add_point(by_file[point.file or "<unknown>"], point)
        if point.module is not None:
            add_point(by_module[point.module], point)

    return {
        "overall": ratio_dict(overall),
        "by_kind": {key: ratio_dict(value) for key, value in sorted(by_kind.items())},
        "by_file": {key: ratio_dict(value) for key, value in sorted(by_file.items())},
        "by_module": {key: ratio_dict(value) for key, value in sorted(by_module.items())},
    }


def add_point(counter: Counter[str], point: RtlStructuralCoveragePoint) -> None:
    counter["total"] += 1
    if point.hit:
        counter["hit"] += 1
    else:
        counter["uncovered"] += 1


def ratio_dict(counter: Counter[str]) -> dict[str, Any]:
    total = counter["total"]
    hit = counter["hit"]
    return {
        "total": total,
        "hit": hit,
        "uncovered": counter["uncovered"],
        "coverage": round(hit / total, 6) if total else None,
    }


def sorted_uncovered(points: list[RtlStructuralCoveragePoint]) -> list[RtlStructuralCoveragePoint]:
    return sorted(
        (point for point in points if not point.hit),
        key=lambda point: (
            point.kind,
            point.file,
            point.line if point.line is not None else -1,
            point.object or "",
        ),
    )


def module_from_page(page: str) -> str | None:
    if "/" not in page:
        return None
    return page.rsplit("/", 1)[-1] or None


def parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
