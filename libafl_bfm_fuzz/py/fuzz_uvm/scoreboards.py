from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fuzz_bfm.target_config import TargetConfig

if TYPE_CHECKING:
    from fuzz_uvm.transactions import ReplayRecord


@dataclass(frozen=True)
class ScoreboardFailure:
    index: int
    line_no: int
    reason: str


class ResultScoreboard:
    """LLM-generated result scoreboard for actual/expected replay records."""

    def __init__(self, target: str, config: TargetConfig | None = None):
        self.target = target
        self.config = config
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
        if record.result.actual != record.result.expected:
            return self._failure(
                record,
                f"actual={record.result.actual} expected={record.result.expected} detail={record.result.detail}",
            )
        return None

    def _failure(self, record: ReplayRecord, reason: str) -> ScoreboardFailure:
        return ScoreboardFailure(
            index=record.index,
            line_no=record.case.line_no,
            reason=reason,
        )


def build_scoreboard(config: TargetConfig) -> ResultScoreboard:
    if config.scoreboard is None:
        return ResultScoreboard(config.name, config=config)

    from fuzz_bfm.plugin_loader import build_plugin

    return build_plugin(config.scoreboard, target=config.name, config=config)
