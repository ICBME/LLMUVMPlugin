from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

from fuzz_bfm.plugin_loader import build_driver
from fuzz_bfm.target_config import TargetConfig
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator
from fuzz_pipeline.harness_runtime_actions import (
    HarnessRuntimeActionManager,
    ScoreboardCheckRuntime,
)
from fuzz_uvm.functional_coverage import build_coverage_model
from fuzz_uvm.ref_models import build_ref_model
from fuzz_uvm.scoreboards import build_scoreboard
from fuzz_uvm.transactions import ReplayRecord


@dataclass(frozen=True)
class ReplayPluginBundle:
    """Business plugin builders for a replay target."""

    config: TargetConfig

    def build_driver(self) -> Any:
        return build_driver(self.config)

    def build_ref_model(self) -> Any:
        return build_ref_model(self.config)

    def build_scoreboard(self) -> Any:
        return build_scoreboard(self.config)

    def build_coverage_model(self) -> Any:
        return build_coverage_model(self.config.name, config=self.config)


class ReplayStageAdapter:
    """Connector-wrapped replay stages around pure plugin operations."""

    def __init__(
        self,
        config: TargetConfig,
        orchestrator: ReplayPipelineOrchestrator | None = None,
        plugins: ReplayPluginBundle | None = None,
    ):
        self.config = config
        self.orchestrator = orchestrator or ReplayPipelineOrchestrator.from_env(config=config)
        self.plugins = plugins or ReplayPluginBundle(config)

    def build_ref_model(self) -> Any:
        return self.orchestrator.build_ref_model(
            self.config,
            self.plugins.build_ref_model,
        )

    def build_replay_driver(self) -> Any:
        return self.orchestrator.build_replay_driver(
            self.config,
            self.plugins.build_driver,
        )

    def build_scoreboard(self) -> Any:
        return self.orchestrator.build_scoreboard(
            self.config,
            self.plugins.build_scoreboard,
        )

    def build_coverage_model(self) -> Any:
        return self.orchestrator.build_functional_coverage(
            self.config,
            self.plugins.build_coverage_model,
        )

    def functional_coverage_output(self) -> Path:
        return self.orchestrator.functional_coverage_output(self.config)

    async def reset_driver(self, target_driver: Any) -> None:
        await self.orchestrator.reset_driver(target_driver.reset)

    async def execute_case(self, target_driver: Any, case: Any, *, index: int) -> Any:
        return await self.orchestrator.execute_case(
            lambda: target_driver.execute(case),
            case,
            index=index,
        )

    def predict_ref_model(self, ref_model: Any, case: Any, *, index: int) -> Any:
        return self.orchestrator.predict_ref_model(
            lambda: ref_model.predict(case),
            case,
            index=index,
        )

    def scoreboard_write(
        self,
        checker: Any,
        record: ReplayRecord,
    ) -> Any:
        return self.orchestrator.scoreboard_write(
            lambda: checker.write(record),
            record,
            summary=checker.summary,
        )

    def scoreboard_check(self, checker: Any) -> Any:
        return self.orchestrator.scoreboard_check(checker.check)

    def scoreboard_summary(self, checker: Any) -> dict[str, Any]:
        return self.orchestrator.scoreboard_summary(checker.summary)

    def coverage_sample(
        self,
        model: Any,
        record: ReplayRecord,
        sampler: Any,
    ) -> Any:
        return self.orchestrator.coverage_sample(
            lambda: sampler(model, record),
            record,
            summary=model.to_json,
        )

    def coverage_export(
        self,
        summary: dict[str, Any],
        output_path: Path,
        writer: Any,
    ) -> dict[str, Any]:
        return self.orchestrator.coverage_export(
            lambda: writer(summary, output_path),
            output_path=output_path,
        )


class ObservableReplayDriverAdapter:
    def __init__(
        self,
        config: TargetConfig,
        orchestrator: ReplayPipelineOrchestrator | None = None,
        stage_adapter: ReplayStageAdapter | None = None,
    ):
        self.config = config
        self.stage = stage_adapter or ReplayStageAdapter(config, orchestrator)
        self.ref_model = self.stage.build_ref_model()
        self.target_driver = self.stage.build_replay_driver()
        self.runtime_actions = HarnessRuntimeActionManager.from_env()

    async def reset(self) -> None:
        await self.runtime_actions.before_reset(driver=self.target_driver)
        await self.stage.reset_driver(self.target_driver)
        await self.runtime_actions.after_reset(driver=self.target_driver)

    async def execute(self, case: Any, *, index: int) -> Any:
        await self.runtime_actions.before_case(
            index=index,
            case=case,
            driver=self.target_driver,
        )
        result = await self.stage.execute_case(self.target_driver, case, index=index)
        if self.ref_model is None:
            await self.runtime_actions.sample_after_execute(
                index=index,
                case=case,
                result=result,
                driver=self.target_driver,
            )
            return result
        expected = self.stage.predict_ref_model(self.ref_model, case, index=index)
        await self.runtime_actions.after_ref_model(
            index=index,
            case=case,
            expected=expected,
            driver=self.target_driver,
        )
        final_result = replace(result, expected=expected.expected)
        await self.runtime_actions.sample_after_execute(
            index=index,
            case=case,
            result=final_result,
            driver=self.target_driver,
        )
        return final_result


class ObservableScoreboardAdapter:
    def __init__(
        self,
        config: TargetConfig,
        orchestrator: ReplayPipelineOrchestrator | None = None,
        stage_adapter: ReplayStageAdapter | None = None,
    ):
        self.stage = stage_adapter or ReplayStageAdapter(config, orchestrator)
        self.checker = self.stage.build_scoreboard()
        self.runtime_actions = HarnessRuntimeActionManager.for_scoreboard_from_env()
        runtime_checks = self.runtime_actions.runtime_of_type(ScoreboardCheckRuntime)
        self.runtime_checks = (
            runtime_checks
            if isinstance(runtime_checks, ScoreboardCheckRuntime)
            else ScoreboardCheckRuntime.from_env()
        )

    def write(self, record: ReplayRecord) -> None:
        self.stage.scoreboard_write(self.checker, record)
        self.runtime_actions.after_scoreboard_record_sync(
            index=record.index,
            case=record.case,
            result=record.result,
            record=record,
        )

    def check(self) -> None:
        self.stage.scoreboard_check(self.checker)
        self.runtime_actions.finalize_sync()

    def summary(self) -> dict[str, Any]:
        summary = self.stage.scoreboard_summary(self.checker)
        if self.runtime_checks.config is not None:
            summary = dict(summary)
            summary["harness_scoreboard_checks"] = self.runtime_checks.summary()
        return summary


class ObservableCoverageAdapter:
    def __init__(
        self,
        config: TargetConfig,
        output_path: Path | None = None,
        orchestrator: ReplayPipelineOrchestrator | None = None,
        stage_adapter: ReplayStageAdapter | None = None,
    ):
        self.stage = stage_adapter or ReplayStageAdapter(config, orchestrator)
        self.model = self.stage.build_coverage_model()
        self.output_path = output_path or self.stage.functional_coverage_output()

    def sample_record(self, record: ReplayRecord) -> None:
        self.stage.coverage_sample(
            self.model,
            record,
            self._sample_record,
        )

    def export_summary(self) -> dict[str, Any]:
        summary = self.model.to_json()
        return self.stage.coverage_export(
            summary,
            output_path=self.output_path,
            writer=self._write_summary,
        )

    def _sample_record(self, model: Any, record: ReplayRecord) -> None:
        if callable(getattr(model, "sample_record", None)):
            model.sample_record(record)
        elif record.error is None:
            model.sample(record.case)

    def _write_summary(
        self,
        summary: dict[str, Any],
        output_path: Path,
    ) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return summary
