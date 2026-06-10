from __future__ import annotations

from pyuvm import ConfigDB, uvm_analysis_port, uvm_driver, uvm_subscriber

from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator
from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.observable import (
    ObservableCoverageAdapter,
    ObservableReplayDriverAdapter,
    ObservableScoreboardAdapter,
)
from fuzz_uvm.transactions import FuzzSeqItem, ReplayRecord


class ReplayDriver(uvm_driver):
    def build_phase(self) -> None:
        self.ap = uvm_analysis_port("ap", self)
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.driver_adapter = ObservableReplayDriverAdapter(
            config,
            orchestrator=_replay_orchestrator_from_config_db(self),
        )

    async def run_phase(self) -> None:
        await self.driver_adapter.reset()
        while True:
            item: FuzzSeqItem = await self.seq_item_port.get_next_item()
            try:
                result = await self.driver_adapter.execute(item.case, index=item.index)
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
        self.scoreboard_adapter = ObservableScoreboardAdapter(
            config,
            orchestrator=_replay_orchestrator_from_config_db(self),
        )

    def write(self, record: ReplayRecord) -> None:
        self.scoreboard_adapter.write(record)

    def check_phase(self) -> None:
        self.scoreboard_adapter.check()

    def report_phase(self) -> None:
        summary = self.scoreboard_adapter.summary()
        self.logger.info(
            "Replay scoreboard: checked=%d failures=%d",
            summary["checked"],
            summary["failures"],
        )


class FunctionalCoverageSubscriber(uvm_subscriber):
    def build_phase(self) -> None:
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.coverage_adapter = ObservableCoverageAdapter(
            config,
            orchestrator=_replay_orchestrator_from_config_db(self),
        )

    def write(self, record: ReplayRecord) -> None:
        self.coverage_adapter.sample_record(record)

    def report_phase(self) -> None:
        summary = self.coverage_adapter.export_summary()
        self.logger.info(
            "UVM functional coverage: target=%s total_cases=%d out=%s",
            summary["target"],
            summary["total_cases"],
            self.coverage_adapter.output_path,
        )


def _replay_orchestrator_from_config_db(
    component,
) -> ReplayPipelineOrchestrator | None:
    try:
        return ConfigDB().get(component, "", "FUZZ_REPLAY_ORCHESTRATOR")
    except Exception:  # noqa: BLE001 - standalone component tests may not install one
        return None
