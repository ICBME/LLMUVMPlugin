from __future__ import annotations

import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
import pyuvm
from pyuvm import uvm_test

from .corpus import load_cases
from .plugin_loader import build_driver
from .target_config import load_target_config


@pyuvm.test()
class LibAflBfmReplayTest(uvm_test):
    async def run_phase(self):
        self.raise_objection()
        try:
            clock = Clock(cocotb.top.clk, 1, "ns")
            cocotb.start_soon(clock.start())

            target = os.getenv("FUZZ_TARGET", "tinyalu")
            config = load_target_config(target)
            corpus = Path(os.getenv("LIBAFL_CORPUS", f"coverage/{target}_corpus.jsonl"))
            cases = load_cases(corpus, target, config=config)
            driver = build_driver(config)

            self.logger.info(
                "LibAFL BFM replay: target=%s driver=%s corpus=%s total_cases=%d",
                target,
                config.driver,
                corpus,
                len(cases),
            )

            await driver.reset()
            for idx, case in enumerate(cases):
                result = await driver.execute(case)
                self.logger.info(
                    "case %d line=%d %s actual=%s expected=%s origin=%s",
                    idx,
                    case.line_no,
                    result.detail,
                    result.actual,
                    result.expected,
                    case.data.get("origin", "libafl"),
                )
        finally:
            self.drop_objection()
