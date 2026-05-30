from __future__ import annotations

import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
import pyuvm
from pyuvm import uvm_test

from .aes_driver import AesDriver
from .corpus import load_cases
from .sha256_driver import Sha256Driver
from .tinyalu_driver import TinyAluDriver


def build_driver(target: str):
    if target == "tinyalu":
        return TinyAluDriver()
    if target == "aes":
        return AesDriver()
    if target == "sha256":
        return Sha256Driver()
    raise ValueError(f"unsupported target {target!r}")


@pyuvm.test()
class LibAflBfmReplayTest(uvm_test):
    async def run_phase(self):
        self.raise_objection()
        try:
            clock = Clock(cocotb.top.clk, 1, "ns")
            cocotb.start_soon(clock.start())

            target = os.getenv("FUZZ_TARGET", "tinyalu")
            corpus = Path(os.getenv("LIBAFL_CORPUS", f"coverage/{target}_corpus.jsonl"))
            cases = load_cases(corpus, target)
            driver = build_driver(target)

            self.logger.info(
                "LibAFL BFM replay: target=%s corpus=%s total_cases=%d",
                target,
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

