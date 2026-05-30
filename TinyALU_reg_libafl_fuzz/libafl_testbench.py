"""pyUVM replay testbench for the TinyALU_reg LibAFL corpus.

The LibAFL fuzzer writes semantic TinyALU transactions as JSONL. This testbench
keeps the RTL-facing boundary identical to the Hypothesis version: each case is
played through the register BFM and checked against the Python ALU model.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

import cocotb
from cocotb.clock import Clock

import pyuvm
from pyuvm import uvm_test


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


@dataclass(frozen=True)
class TinyAluFuzzCase:
    """A legal semantic TinyALU transaction emitted by the LibAFL fuzzer."""

    a: int
    b: int
    op: int
    origin: str = "libafl"


@dataclass(frozen=True)
class ReplayConfig:
    corpus_path: Path

    @classmethod
    def from_env(cls) -> "ReplayConfig":
        corpus = os.getenv("LIBAFL_CORPUS", "coverage/libafl_fuzz_corpus.jsonl")
        return cls(corpus_path=Path(corpus))


def load_cases(path: Path) -> list[TinyAluFuzzCase]:
    if not path.exists():
        raise FileNotFoundError(
            f"LibAFL corpus {path} does not exist. Run `make generate-corpus` first."
        )

    cases: list[TinyAluFuzzCase] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        case = TinyAluFuzzCase(
            a=int(data["a"]),
            b=int(data["b"]),
            op=int(data["op"]),
            origin=str(data.get("origin", "libafl")),
        )
        validate_case(case, path, line_no)
        cases.append(case)

    if not cases:
        raise ValueError(f"LibAFL corpus {path} did not contain any cases")
    return cases


def validate_case(case: TinyAluFuzzCase, path: Path, line_no: int) -> None:
    if not 0 <= case.a <= 0xFF:
        raise ValueError(f"{path}:{line_no}: A is not an 8-bit value: {case.a}")
    if not 0 <= case.b <= 0xFF:
        raise ValueError(f"{path}:{line_no}: B is not an 8-bit value: {case.b}")
    if case.op not in {int(op) for op in Ops}:
        raise ValueError(f"{path}:{line_no}: illegal TinyALU op: {case.op}")


class TinyAluRegBfmClient:
    """Semantic BFM facade used by the replay test."""

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
class LibAflReplayTest(uvm_test):
    """Run LibAFL-generated legal ALU transactions through the BFM."""

    async def run_phase(self):
        self.raise_objection()
        try:
            clock = Clock(cocotb.top.clk, 1, "ns")
            cocotb.start_soon(clock.start())

            config = ReplayConfig.from_env()
            cases = load_cases(config.corpus_path)
            client = TinyAluRegBfmClient()
            covered_ops: set[Ops] = set()

            self.logger.info(
                "LibAFL replay: "
                f"corpus={config.corpus_path} total_cases={len(cases)}"
            )

            await client.reset()
            for idx, case in enumerate(cases):
                op = Ops(case.op)
                covered_ops.add(op)
                actual = await client.execute(case)
                expected = alu_prediction(case.a, case.b, op)
                self.logger.info(
                    "LibAFL replay case "
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
