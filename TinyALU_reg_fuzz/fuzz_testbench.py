"""pyUVM testbench that plays Hypothesis fuzz cases through TinyALU's BFM.

This file does not import the original TinyALU_reg testbench, so none of the
original tests or RAL classes are affected. It only reuses the original BFM
utility module and wraps the register protocol behind a semantic execute call.
"""

from __future__ import annotations

from pathlib import Path
import sys

import cocotb
from cocotb.clock import Clock

import pyuvm
from pyuvm import uvm_test

from hypothesis_fuzz import FuzzConfig, TinyAluFuzzCase, generate_tinyalu_cases


THIS_DIR = Path(__file__).resolve().parent
BASE_DIR = THIS_DIR.parent / "pyuvm" / "examples" / "TinyALU_reg"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tinyalu_utils import Ops, TinyAluBfm, alu_prediction  # noqa: E402


ALU_REG_SRC_ADDR = 0x0
ALU_REG_RESULT_ADDR = 0x2
ALU_REG_CMD_ADDR = 0x4
ALU_REG_SRC_DATA1_SHIFT = 8
ALU_REG_CMD_START_SHIFT = 5
ALU_REG_CMD_DONE_SHIFT = 6


class TinyAluRegBfmClient:
    """Semantic BFM facade used by the fuzz test.

    The fuzz layer supplies only A, B, and op. Register writes, start/done
    polling, and result reads stay below this boundary.
    """

    def __init__(self, poll_limit: int = 32):
        self.bfm = TinyAluBfm()
        self.poll_limit = poll_limit

    async def reset(self) -> None:
        await self.bfm.reset()

    async def execute(self, case: TinyAluFuzzCase) -> int:
        await self.bfm.SW_WRITE(ALU_REG_CMD_ADDR, 0)
        await self.bfm.SW_WRITE(
            ALU_REG_SRC_ADDR,
            (case.b << ALU_REG_SRC_DATA1_SHIFT) | case.a,
        )
        await self.bfm.SW_WRITE(
            ALU_REG_CMD_ADDR,
            (1 << ALU_REG_CMD_START_SHIFT) | case.op,
        )

        for _ in range(self.poll_limit):
            cmd = await self.bfm.SW_READ(ALU_REG_CMD_ADDR)
            if ((cmd >> ALU_REG_CMD_DONE_SHIFT) & 1) == 1:
                return await self.bfm.SW_READ(ALU_REG_RESULT_ADDR)

        op = Ops(case.op).name
        raise TimeoutError(
            f"ALU operation did not finish: A=0x{case.a:02x} "
            f"B=0x{case.b:02x} op={op}"
        )


@pyuvm.test()
class HypothesisFuzzTest(uvm_test):
    """Run legal Hypothesis-generated ALU transactions through the BFM."""

    async def run_phase(self):
        self.raise_objection()
        try:
            clock = Clock(cocotb.top.clk, 1, "ns")
            cocotb.start_soon(clock.start())

            config = FuzzConfig.from_env()
            cases = generate_tinyalu_cases(list(Ops), config)
            client = TinyAluRegBfmClient()
            covered_ops: set[Ops] = set()

            self.logger.info(
                "Hypothesis fuzz: "
                f"seed={config.seed} "
                f"hypothesis_examples={config.max_examples} "
                f"total_cases={len(cases)}"
            )

            await client.reset()
            for idx, case in enumerate(cases):
                op = Ops(case.op)
                covered_ops.add(op)
                actual = await client.execute(case)
                expected = alu_prediction(case.a, case.b, op)
                self.logger.info(
                    "Hypothesis fuzz case "
                    f"{idx}: A=0x{case.a:02x} B=0x{case.b:02x} "
                    f"op={op.name} actual=0x{actual:04x} "
                    f"expected=0x{expected:04x} origin={case.origin}"
                )
                assert actual == expected, (
                    f"case {idx} failed: A=0x{case.a:02x} B=0x{case.b:02x} "
                    f"op={op.name} actual=0x{actual:04x} "
                    f"expected=0x{expected:04x}"
                )

            missing_ops = set(Ops) - covered_ops
            assert not missing_ops, f"missed op coverage: {missing_ops}"
        finally:
            self.drop_objection()
