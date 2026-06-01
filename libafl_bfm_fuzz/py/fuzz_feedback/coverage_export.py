from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any


COVERAGE_EXPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CoveragePoint:
    """One normalized coverage point from a tool or coverage model."""

    kind: str
    file: str
    line: int | None
    count: int
    module: str | None = None
    object: str | None = None
    source: str = "unknown"
    domain: str = "rtl_structure"
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def hit(self) -> bool:
        return self.count > 0

    @property
    def id(self) -> str:
        return stable_id(
            self.domain,
            self.source,
            self.kind,
            self.file,
            self.line,
            self.module,
            self.object,
            self.name,
        )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        data["hit"] = self.hit
        return data


@dataclass(frozen=True)
class CoverageExport:
    """Structured coverage export consumed by gap builders and advisors."""

    domain: str
    points: tuple[CoveragePoint, ...]
    sources: dict[str, str]
    target: str | None = None
    schema_version: int = COVERAGE_EXPORT_SCHEMA_VERSION

    def to_json(
        self,
        *,
        max_points: int | None = 0,
        max_uncovered_points: int = 160,
    ) -> dict[str, Any]:
        totals = summarize_points(list(self.points))
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "domain": self.domain,
            "target": self.target,
            "sources": self.sources,
            "point_count": len(self.points),
            "uncovered_point_count": sum(1 for point in self.points if not point.hit),
            "totals": totals["overall"],
            "by_kind": totals["by_kind"],
            "by_file": totals["by_file"],
            "by_module": totals["by_module"],
            "uncovered_points": [
                point.to_json()
                for point in sorted_uncovered_points(list(self.points))[:max_uncovered_points]
            ],
        }
        if max_points is None:
            data["points"] = [point.to_json() for point in self.points]
        elif max_points > 0:
            data["points"] = [point.to_json() for point in self.points[:max_points]]
        return data


def summarize_points(points: list[CoveragePoint]) -> dict[str, Any]:
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


def add_point(counter: Counter[str], point: CoveragePoint) -> None:
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


def sorted_uncovered_points(points: list[CoveragePoint]) -> list[CoveragePoint]:
    return sorted(
        (point for point in points if not point.hit),
        key=lambda point: (
            point.kind,
            point.file,
            point.line if point.line is not None else -1,
            point.object or "",
            point.name or "",
        ),
    )


def stable_id(*parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(text.encode()).hexdigest()[:16]
