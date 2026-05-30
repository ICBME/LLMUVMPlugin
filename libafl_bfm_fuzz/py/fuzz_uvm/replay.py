from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import cocotb
from cocotb.clock import Clock
from pyuvm import (
    ConfigDB,
    uvm_analysis_port,
    uvm_driver,
    uvm_env,
    uvm_sequence,
    uvm_sequence_item,
    uvm_sequencer,
    uvm_subscriber,
    uvm_test,
)

from fuzz_bfm.bfm_base import ReplayResult
from fuzz_bfm.corpus import FuzzCase, load_cases
from fuzz_bfm.plugin_loader import build_driver
from fuzz_bfm.target_config import TargetConfig, load_target_config
from fuzz_uvm.functional_coverage import FunctionalCoverageModel


@dataclass(frozen=True)
class ReplayRecord:
    index: int
    case: FuzzCase
    result: ReplayResult | None = None
    error: str | None = None


class FuzzSeqItem(uvm_sequence_item):
    def __init__(self, name: str, case: FuzzCase, index: int):
        super().__init__(name)
        self.case = case
        self.index = index
        self.result: ReplayResult | None = None
        self.error: str | None = None

    def __str__(self) -> str:
        origin = self.case.data.get("origin", "libafl")
        return f"{self.get_name()} target={self.case.target} line={self.case.line_no} origin={origin}"


class CorpusReplaySequence(uvm_sequence):
    def __init__(self, name: str, cases: list[FuzzCase]):
        super().__init__(name)
        self.cases = cases

    async def body(self) -> None:
        for index, case in enumerate(self.cases):
            item = FuzzSeqItem(f"case_{index}", case, index)
            await self.start_item(item)
            await self.finish_item(item)


class ReplayDriver(uvm_driver):
    def build_phase(self) -> None:
        self.ap = uvm_analysis_port("ap", self)
        config: TargetConfig = ConfigDB().get(self, "", "FUZZ_TARGET_CONFIG")
        self.target_driver = build_driver(config)

    async def run_phase(self) -> None:
        await self.target_driver.reset()
        while True:
            item: FuzzSeqItem = await self.seq_item_port.get_next_item()
            try:
                result = await self.target_driver.execute(item.case)
                item.result = result
                self.ap.write(ReplayRecord(item.index, item.case, result=result))
                self.logger.info(
                    "case %d line=%d %s actual=%s expected=%s origin=%s",
                    item.index,
                    item.case.line_no,
                    result.detail,
                    result.actual,
                    result.expected,
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
        self.records: list[ReplayRecord] = []
        self.failures: list[ReplayRecord] = []

    def write(self, record: ReplayRecord) -> None:
        self.records.append(record)
        if record.error is not None:
            self.failures.append(record)

    def check_phase(self) -> None:
        if self.failures:
            first = self.failures[0]
            raise AssertionError(
                f"Replay scoreboard saw {len(self.failures)} failures; "
                f"first line={first.case.line_no} error={first.error}"
            )

    def report_phase(self) -> None:
        self.logger.info(
            "Replay scoreboard: checked=%d failures=%d",
            len(self.records),
            len(self.failures),
        )


class FunctionalCoverageSubscriber(uvm_subscriber):
    def build_phase(self) -> None:
        target: str = ConfigDB().get(self, "", "FUZZ_TARGET")
        self.model = FunctionalCoverageModel(target)
        default_path = Path("coverage") / f"{target}_uvm_functional_coverage.json"
        self.output_path = Path(os.getenv("UVM_FUNCTIONAL_COVERAGE_OUT", str(default_path)))

    def write(self, record: ReplayRecord) -> None:
        if record.error is None:
            self.model.sample(record.case)

    def report_phase(self) -> None:
        summary = self.model.to_json()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(json_dumps(summary))
        self.logger.info(
            "UVM functional coverage: target=%s total_cases=%d out=%s",
            summary["target"],
            summary["total_cases"],
            self.output_path,
        )


class FuzzEnv(uvm_env):
    def build_phase(self) -> None:
        self.seqr = uvm_sequencer("seqr", self)
        self.driver = ReplayDriver("driver", self)
        self.scoreboard = ReplayScoreboard("scoreboard", self)
        self.functional_coverage = FunctionalCoverageSubscriber("functional_coverage", self)

    def connect_phase(self) -> None:
        self.driver.seq_item_port.connect(self.seqr.seq_item_export)
        self.driver.ap.connect(self.scoreboard.analysis_export)
        self.driver.ap.connect(self.functional_coverage.analysis_export)
        ConfigDB().set(None, "*", "FUZZ_SEQUENCER", self.seqr)


class LibAflUvmReplayTest(uvm_test):
    def build_phase(self) -> None:
        self.target = os.getenv("FUZZ_TARGET", "tinyalu")
        self.config = load_target_config(self.target)
        self.corpus = Path(os.getenv("LIBAFL_CORPUS", f"coverage/{self.target}_corpus.jsonl"))
        self.cases = load_cases(self.corpus, self.target, config=self.config)
        ConfigDB().set(None, "*", "FUZZ_TARGET", self.target)
        ConfigDB().set(None, "*", "FUZZ_TARGET_CONFIG", self.config)
        self.env = FuzzEnv("env", self)

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            clock = Clock(cocotb.top.clk, 1, "ns")
            cocotb.start_soon(clock.start())

            self.logger.info(
                "LibAFL UVM replay: target=%s driver=%s corpus=%s total_cases=%d",
                self.target,
                self.config.driver,
                self.corpus,
                len(self.cases),
            )
            sequence = CorpusReplaySequence("corpus_replay", self.cases)
            await sequence.start(self.env.seqr)
        finally:
            self.drop_objection()


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, sort_keys=True) + "\n"
