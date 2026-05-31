from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

from pyuvm import ConfigDB, uvm_analysis_port, uvm_driver, uvm_subscriber

from fuzz_bfm.plugin_loader import build_driver
from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.functional_coverage import build_coverage_model
from fuzz_uvm.ref_models import build_ref_model
from fuzz_uvm.scoreboards import build_scoreboard
from fuzz_uvm.transactions import FuzzSeqItem, ReplayRecord


class ReplayDriver(uvm_driver):
    def build_phase(self) -> None:
        self.ap = uvm_analysis_port("ap", self)
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.ref_model = build_ref_model(config)
        self.target_driver = build_driver(config)

    async def run_phase(self) -> None:
        await self.target_driver.reset()
        while True:
            item: FuzzSeqItem = await self.seq_item_port.get_next_item()
            try:
                result = await self.target_driver.execute(item.case)
                if self.ref_model is not None:
                    expected = self.ref_model.predict(item.case)
                    result = replace(result, expected=expected.expected)
                item.result = result
                self.ap.write(ReplayRecord(item.index, item.case, result=result))
                self.logger.info(
                    "case %d line=%d %s actual=%s expected=%s origin=%s",
                    item.index,
                    item.case.line_no,
                    result.detail,
                    result.actual,
                    result.expected if result.expected is not None else "<unset>",
                    item.case.data.get("origin", "libafl"),
                )
            except Exception as exc:  # noqa: BLE001 - preserve DUT failure details in the analysis path
                item.error = f"{type(exc).__name__}: {exc}"
                self.ap.write(ReplayRecord(item.index, item.case, error=item.error))
                raise
            finally:
                self.seq_item_port.item_done()


class ReplayScoreboard(uvm_subscriber):
    def build_phase(self) -> None:
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.checker = build_scoreboard(config)

    def write(self, record: ReplayRecord) -> None:
        self.checker.write(record)

    def check_phase(self) -> None:
        self.checker.check()

    def report_phase(self) -> None:
        summary = self.checker.summary()
        self.logger.info(
            "Replay scoreboard: checked=%d failures=%d",
            summary["checked"],
            summary["failures"],
        )


class FunctionalCoverageSubscriber(uvm_subscriber):
    def build_phase(self) -> None:
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.model = build_coverage_model(config.name, config=config)
        default_path = Path("coverage") / f"{config.name}_uvm_functional_coverage.json"
        self.output_path = Path(os.getenv("UVM_FUNCTIONAL_COVERAGE_OUT", str(default_path)))

    def write(self, record: ReplayRecord) -> None:
        if record.error is None:
            self.model.sample(record.case)

    def report_phase(self) -> None:
        summary = self.model.to_json()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        self.logger.info(
            "UVM functional coverage: target=%s total_cases=%d out=%s",
            summary["target"],
            summary["total_cases"],
            self.output_path,
        )
