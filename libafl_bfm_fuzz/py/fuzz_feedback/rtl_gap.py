from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Any

from .coverage_export import CoverageExport, CoveragePoint, stable_id


RTL_GAP_DOMAIN = "rtl_gap"
RTL_GAP_SCHEMA_VERSION = 1
RTL_GAP_KIND_PRIORITY = {
    "branch": 100,
    "expression": 90,
    "line": 80,
    "fsm": 70,
    "user": 60,
    "toggle": 40,
}
RTL_GAP_PRIORITY_ORDER = tuple(
    kind for kind, _priority in sorted(RTL_GAP_KIND_PRIORITY.items(), key=lambda item: -item[1])
)
RTL_GAP_KEYWORDS = (
    "address",
    "addr",
    "case",
    "state",
    "mode",
    "op",
    "cmd",
    "key",
    "block",
    "round",
    "len",
    "length",
    "size",
    "pad",
    "padding",
    "next",
    "init",
    "reset",
    "error",
    "valid",
    "ready",
    "read",
    "write",
    "we",
    "cs",
)


@dataclass(frozen=True)
class RtlCoverageGap:
    """Actionable grouping of one or more uncovered structural points."""

    id: str
    primary_kind: str
    file: str
    line: int | None
    module: str | None
    kinds: dict[str, int]
    point_count: int
    objects: tuple[str, ...]
    object_count: int
    priority: int
    evidence: dict[str, Any]
    advisor_hints: tuple[dict[str, str], ...]
    code: str = ""
    context: tuple[dict[str, Any], ...] = ()
    actionability: str = "unknown"
    source: str = "rtl_structure"
    domain: str = RTL_GAP_DOMAIN
    schema_version: int = RTL_GAP_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def build_rtl_gap_export(
    coverage_export: CoverageExport,
    *,
    max_gaps: int = 48,
    source_context_radius: int = 2,
) -> dict[str, Any]:
    return build_rtl_gap_summary(
        list(coverage_export.points),
        target=coverage_export.target,
        max_gaps=max_gaps,
        source_context_radius=source_context_radius,
    )


def build_rtl_gap_summary(
    points: list[CoveragePoint],
    *,
    target: str | None = None,
    max_gaps: int = 48,
    source_context_radius: int = 2,
) -> dict[str, Any]:
    uncovered = [point for point in points if not point.hit]
    gaps = sorted_rtl_gaps(
        uncovered,
        source_context_radius=source_context_radius,
    )
    by_kind = Counter(point.kind for point in uncovered)
    by_file = Counter(gap.file or "<unknown>" for gap in gaps)
    by_module = Counter(gap.module or "<unknown>" for gap in gaps)
    return {
        "schema_version": RTL_GAP_SCHEMA_VERSION,
        "domain": RTL_GAP_DOMAIN,
        "target": target,
        "total": len(gaps),
        "total_points": len(uncovered),
        "priority_order": list(RTL_GAP_PRIORITY_ORDER),
        "by_kind": dict(sorted(by_kind.items())),
        "by_file": dict(sorted(by_file.items())),
        "by_module": dict(sorted(by_module.items())),
        "top_gaps": [gap.to_json() for gap in gaps[:max_gaps]],
    }


def sorted_rtl_gaps(
    uncovered_points: list[CoveragePoint],
    *,
    source_context_radius: int = 2,
) -> list[RtlCoverageGap]:
    source_cache: dict[str, list[str]] = {}
    grouped: dict[tuple[Any, ...], list[CoveragePoint]] = defaultdict(list)
    for point in uncovered_points:
        grouped[rtl_gap_key(point)].append(point)

    gaps = [
        build_rtl_gap(group_points, source_cache, source_context_radius=source_context_radius)
        for group_points in grouped.values()
    ]
    return sorted(
        gaps,
        key=lambda gap: (
            -gap.priority,
            gap.file,
            gap.line if gap.line is not None else -1,
            gap.module or "",
            gap.primary_kind,
        ),
    )


def rtl_gap_key(point: CoveragePoint) -> tuple[Any, ...]:
    file_name = point.file or "<unknown>"
    module = point.module or "<unknown>"
    if point.line is not None:
        return (file_name, point.line)
    return (file_name, None, module, point.kind, point.object or point.name or "")


def build_rtl_gap(
    points: list[CoveragePoint],
    source_cache: dict[str, list[str]],
    *,
    source_context_radius: int,
) -> RtlCoverageGap:
    representative = points[0]
    module = next((point.module for point in points if point.module is not None), None)
    kinds = Counter(point.kind for point in points)
    primary_kind = select_primary_kind(kinds)
    objects = sorted(
        {
            str(point.object)
            for point in points
            if point.object is not None and str(point.object)
        }
    )
    code, context = source_context(
        representative.file,
        representative.line,
        source_cache,
        radius=source_context_radius,
    )
    evidence = {
        "point_ids": [point.id for point in points],
        "kinds": dict(sorted(kinds.items())),
        "objects": objects[:12],
        "sources": sorted({point.source for point in points}),
    }
    gap_id = stable_id(
        RTL_GAP_DOMAIN,
        representative.file,
        representative.line,
        module,
        primary_kind,
        objects,
    )
    return RtlCoverageGap(
        id=gap_id,
        primary_kind=primary_kind,
        file=representative.file or "<unknown>",
        line=representative.line,
        module=module,
        kinds=dict(sorted(kinds.items())),
        point_count=len(points),
        objects=tuple(objects[:12]),
        object_count=len(objects),
        priority=RTL_GAP_KIND_PRIORITY.get(primary_kind, 10),
        evidence=evidence,
        advisor_hints=tuple(advisor_hints(code, context, objects, module)),
        code=code,
        context=tuple(context),
    )


def select_primary_kind(kinds: Counter[str]) -> str:
    return max(
        kinds,
        key=lambda kind: (
            RTL_GAP_KIND_PRIORITY.get(kind, 10),
            kinds[kind],
            kind,
        ),
    )


def advisor_hints(
    code: str,
    context: list[dict[str, Any]],
    objects: list[str],
    module: str | None,
) -> list[dict[str, str]]:
    text_parts = [code, module or "", *objects]
    text_parts.extend(str(item.get("code", "")) for item in context)
    text = "\n".join(text_parts).lower()

    hints: list[dict[str, str]] = []
    for keyword in RTL_GAP_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", text):
            hints.append({"type": "source_keyword", "value": keyword})

    for signal in sorted(set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_$]*\b", " ".join(objects)))):
        lowered = signal.lower()
        if lowered in {"if", "else", "begin", "end"}:
            continue
        hints.append({"type": "signal_name", "value": signal})
        if len(hints) >= 16:
            break
    return hints[:16]


def source_context(
    file_name: str,
    line_no: int | None,
    source_cache: dict[str, list[str]],
    *,
    radius: int,
) -> tuple[str, list[dict[str, Any]]]:
    if not file_name or line_no is None:
        return "", []
    source = source_cache.setdefault(file_name, read_source_lines(file_name))
    if not source or line_no <= 0 or line_no > len(source):
        return "", []

    start = max(1, line_no - radius)
    end = min(len(source), line_no + radius)
    context = [
        {"line": idx, "code": source[idx - 1].strip()}
        for idx in range(start, end + 1)
    ]
    return source[line_no - 1].strip(), context


def read_source_lines(file_name: str) -> list[str]:
    path = Path(file_name)
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()
