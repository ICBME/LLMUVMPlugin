from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.contracts import (
    ComparatorPlugin,
    DefaultComparator,
    ScoreboardPlugin,
    normalize_comparison,
    validate_comparator_plugin,
    validate_scoreboard_plugin,
)

if TYPE_CHECKING:
    from fuzz_uvm.transactions import ReplayRecord


@dataclass(frozen=True)
class ScoreboardFailure:
    index: int
    line_no: int
    reason: str


class ResultScoreboard:
    """Default result scoreboard for actual/expected replay records."""

    def __init__(
        self,
        target: str,
        config: TargetConfig | None = None,
        comparator: ComparatorPlugin | None = None,
    ):
        self.target = target
        self.config = config
        self.comparator = validate_comparator_plugin(
            comparator or DefaultComparator(),
            spec=f"{target} comparator",
        )
        self.records: list[ReplayRecord] = []
        self.failures: list[ScoreboardFailure] = []

    def write(self, record: ReplayRecord) -> None:
        self.records.append(record)
        failure = self._check_record(record)
        if failure is not None:
            self.failures.append(failure)

    def check(self) -> None:
        if self.failures:
            first = self.failures[0]
            raise AssertionError(
                f"Replay scoreboard saw {len(self.failures)} failures for {self.target}; "
                f"first case={first.index} line={first.line_no}: {first.reason}"
            )

    def summary(self) -> dict[str, int | str]:
        return {
            "target": self.target,
            "checked": len(self.records),
            "failures": len(self.failures),
        }

    def _check_record(self, record: ReplayRecord) -> ScoreboardFailure | None:
        if record.error is not None:
            return self._failure(record, record.error)
        if record.result is None:
            return self._failure(record, "missing replay result")
        if record.result.expected is None:
            return self._failure(record, f"missing expected result for {record.result.detail}")
        comparison = normalize_comparison(
            self.comparator.compare(record.result.actual, record.result.expected, record),
            spec=f"{self.target} comparator",
        )
        if not comparison.passed:
            reason = comparison.reason or (
                f"actual={record.result.actual!r} expected={record.result.expected!r}"
            )
            details = []
            if comparison.detail:
                details.append(f"comparison={comparison.detail}")
            if record.result.detail:
                details.append(f"replay={record.result.detail}")
            if details:
                reason = f"{reason} {' '.join(details)}"
            return self._failure(record, reason)
        return None

    def _failure(self, record: ReplayRecord, reason: str) -> ScoreboardFailure:
        return ScoreboardFailure(
            index=record.index,
            line_no=record.case.line_no,
            reason=reason,
        )


def build_scoreboard(config: TargetConfig) -> ScoreboardPlugin:
    if config.scoreboard is None:
        return ResultScoreboard(config.name, config=config)

    from fuzz_bfm.plugin_loader import build_plugin

    plugin = build_plugin(config.scoreboard, target=config.name, config=config)
    return validate_scoreboard_plugin(plugin, spec=config.scoreboard)


__all__ = [
    "ResultScoreboard",
    "ScoreboardFailure",
    "build_scoreboard",
]
