from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from fuzz_bfm.plugin_loader import build_driver
from fuzz_bfm.target_config import TargetConfig
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator
from fuzz_uvm.functional_coverage import build_coverage_model
from fuzz_uvm.ref_models import build_ref_model
from fuzz_uvm.scoreboards import build_scoreboard
from fuzz_uvm.transactions import ReplayRecord


class ObservableReplayDriverAdapter:
    def __init__(
        self,
        config: TargetConfig,
        orchestrator: ReplayPipelineOrchestrator | None = None,
    ):
        self.config = config
        self.orchestrator = orchestrator or ReplayPipelineOrchestrator.from_env(config=config)
        self.ref_model = self.orchestrator.build_ref_model(
            config,
            lambda: build_ref_model(config),
        )
        self.target_driver = self.orchestrator.build_replay_driver(
            config,
            lambda: build_driver(config),
        )

    async def reset(self) -> None:
        await self.orchestrator.reset_driver(self.target_driver.reset)

    async def execute(self, case: Any, *, index: int) -> Any:
        result = await self.orchestrator.execute_case(
            lambda: self.target_driver.execute(case),
            case,
            index=index,
        )
        if self.ref_model is None:
            return result
        expected = self.orchestrator.predict_ref_model(
            lambda: self.ref_model.predict(case),
            case,
            index=index,
        )
        return replace(result, expected=expected.expected)


class ObservableScoreboardAdapter:
    def __init__(
        self,
        config: TargetConfig,
        orchestrator: ReplayPipelineOrchestrator | None = None,
    ):
        self.orchestrator = orchestrator or ReplayPipelineOrchestrator.from_env(config=config)
        self.checker = build_scoreboard(config)

    def write(self, record: ReplayRecord) -> None:
        self.orchestrator.scoreboard_write(
            lambda: self.checker.write(record),
            record,
            summary=self.checker.summary,
        )

    def check(self) -> None:
        self.orchestrator.scoreboard_check(self.checker.check)

    def summary(self) -> dict[str, Any]:
        return self.orchestrator.scoreboard_summary(self.checker.summary)


class ObservableCoverageAdapter:
    def __init__(
        self,
        config: TargetConfig,
        output_path: Path,
        orchestrator: ReplayPipelineOrchestrator | None = None,
    ):
        self.orchestrator = orchestrator or ReplayPipelineOrchestrator.from_env(config=config)
        self.model = build_coverage_model(config.name, config=config)
        self.output_path = output_path

    def sample_record(self, record: ReplayRecord) -> None:
        self.orchestrator.coverage_sample(
            lambda: self._sample_record(record),
            record,
            summary=self.model.to_json,
        )

    def export_summary(self) -> dict[str, Any]:
        summary = self.model.to_json()
        return self.orchestrator.coverage_export(
            lambda: self._write_summary(summary),
            output_path=self.output_path,
        )

    def _sample_record(self, record: ReplayRecord) -> None:
        if callable(getattr(self.model, "sample_record", None)):
            self.model.sample_record(record)
        elif record.error is None:
            self.model.sample(record.case)

    def _write_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return summary
