from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from fuzz_bfm.plugin_loader import build_driver
from fuzz_bfm.target_config import TargetConfig
from fuzz_pipeline.harness import (
    connector_from_env,
    functional_coverage_metrics,
    replay_case_metadata,
    replay_result_metrics,
    scoreboard_metrics,
)
from fuzz_uvm.functional_coverage import build_coverage_model
from fuzz_uvm.ref_models import build_ref_model
from fuzz_uvm.scoreboards import build_scoreboard
from fuzz_uvm.transactions import ReplayRecord


class ObservableReplayDriverAdapter:
    def __init__(self, config: TargetConfig):
        self.config = config
        self.ref_model = connector_from_env(
            "manifest_to_ref_model",
            "target_manifest",
            "ref_model",
        ).run(
            build_ref_model,
            config,
            metrics=lambda value: {"available": value is not None},
            metadata={"target": config.name, "ref_model": config.ref_model or ""},
        )
        self.target_driver = connector_from_env(
            "manifest_to_replay_driver",
            "target_manifest",
            "replay_driver",
        ).run(
            build_driver,
            config,
            metrics=lambda _value: {"driver": config.driver},
            metadata={"target": config.name},
        )
        self.reset_connector = connector_from_env("driver_reset_to_dut", "replay_driver", "dut")
        self.execute_connector = connector_from_env("case_to_dut", "replay_driver", "dut")
        self.ref_model_connector = connector_from_env(
            "case_to_ref_model",
            "replay_driver",
            "ref_model",
        )

    async def reset(self) -> None:
        await self.reset_connector.run_async(
            self.target_driver.reset,
            metrics=lambda _value: {"operation": "reset"},
        )

    async def execute(self, case: Any, *, index: int) -> Any:
        result = await self.execute_connector.run_async(
            self.target_driver.execute,
            case,
            metadata=replay_case_metadata(case, index=index),
            metrics=replay_result_metrics,
        )
        if self.ref_model is None:
            return result
        expected = self.ref_model_connector.run(
            self.ref_model.predict,
            case,
            metadata=replay_case_metadata(case, index=index),
            metrics=lambda value: {
                "has_expected": getattr(value, "expected", None) is not None
            },
        )
        return replace(result, expected=expected.expected)


class ObservableScoreboardAdapter:
    def __init__(self, config: TargetConfig):
        self.checker = build_scoreboard(config)
        self.write_connector = connector_from_env(
            "driver_to_scoreboard",
            "replay_driver",
            "scoreboard",
        )
        self.report_connector = connector_from_env(
            "scoreboard_to_report",
            "scoreboard",
            "scoreboard_report",
        )

    def write(self, record: ReplayRecord) -> None:
        self.write_connector.run(
            self.checker.write,
            record,
            metadata=replay_case_metadata(record.case, index=record.index),
            metrics=lambda _value: scoreboard_metrics(self.checker.summary()),
        )

    def check(self) -> None:
        self.report_connector.run(self.checker.check)

    def summary(self) -> dict[str, Any]:
        return self.report_connector.run(
            self.checker.summary,
            metrics=scoreboard_metrics,
        )


class ObservableCoverageAdapter:
    def __init__(self, config: TargetConfig, output_path: Path):
        self.model = build_coverage_model(config.name, config=config)
        self.output_path = output_path
        self.sample_connector = connector_from_env(
            "driver_to_functional_coverage",
            "replay_driver",
            "functional_coverage",
        )
        self.export_connector = connector_from_env(
            "functional_coverage_to_summary",
            "functional_coverage",
            "functional_coverage_summary",
        )

    def sample_record(self, record: ReplayRecord) -> None:
        self.sample_connector.run(
            self._sample_record,
            record,
            metadata=replay_case_metadata(record.case, index=record.index),
            metrics=lambda _value: functional_coverage_metrics(self.model.to_json()),
        )

    def export_summary(self) -> dict[str, Any]:
        return self.export_connector.run(
            self._write_summary,
            self.model.to_json(),
            outputs={"functional_coverage": self.output_path},
            metrics=functional_coverage_metrics,
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
