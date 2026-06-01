from __future__ import annotations

from pathlib import Path
from typing import Any

from .coverage_export import CoverageExport, CoveragePoint, sorted_uncovered_points
from .rtl_gap import (
    RTL_GAP_DOMAIN,
    RTL_GAP_KIND_PRIORITY,
    RTL_GAP_PRIORITY_ORDER,
    build_rtl_gap_export,
    build_rtl_gap_summary,
)


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


RtlStructuralCoveragePoint = CoveragePoint


def build_rtl_structure_coverage(
    coverage_info: Path | None = None,
    coverage_dat: Path | None = None,
    max_uncovered_points: int = 160,
    max_rtl_gaps: int = 48,
    source_context_radius: int = 2,
    max_export_points: int | None = 0,
    target: str | None = None,
) -> dict[str, Any]:
    export = build_rtl_structure_coverage_export(
        coverage_info=coverage_info,
        coverage_dat=coverage_dat,
        target=target,
    )
    summary = export.to_json(
        max_points=max_export_points,
        max_uncovered_points=max_uncovered_points,
    )
    summary["available_kinds"] = sorted(summary["by_kind"].keys())
    summary["supported_kinds"] = list(RTL_STRUCTURAL_COVERAGE_KINDS)
    summary["coverage_export"] = export.to_json(
        max_points=max_export_points,
        max_uncovered_points=max_uncovered_points,
    )
    summary["rtl_gap_summary"] = build_rtl_gap_export(
        export,
        max_gaps=max_rtl_gaps,
        source_context_radius=source_context_radius,
    )
    return summary


def build_rtl_structure_coverage_export(
    coverage_info: Path | None = None,
    coverage_dat: Path | None = None,
    target: str | None = None,
) -> CoverageExport:
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

    return CoverageExport(
        domain=RTL_STRUCTURAL_COVERAGE_DOMAIN,
        target=target,
        points=tuple(points),
        sources=sources,
    )


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


def sorted_uncovered(points: list[RtlStructuralCoveragePoint]) -> list[RtlStructuralCoveragePoint]:
    return sorted_uncovered_points(points)


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
