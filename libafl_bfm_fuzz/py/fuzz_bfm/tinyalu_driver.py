from __future__ import annotations

from pathlib import Path
import sys

from .bfm_base import ReplayResult
from .corpus import FuzzCase
from .target_config import TargetConfig


THIS_DIR = Path(__file__).resolve()
TINYA_REG_DIR = THIS_DIR.parents[3] / "pyuvm" / "examples" / "TinyALU_reg"
if str(TINYA_REG_DIR) not in sys.path:
    sys.path.insert(0, str(TINYA_REG_DIR))

from tinyalu_utils import Ops, TinyAluBfm, alu_prediction  # noqa: E402


ALU_REG_SRC_ADDR = 0x0
ALU_REG_RESULT_ADDR = 0x2
ALU_REG_CMD_ADDR = 0x4
ALU_REG_SRC_DATA1_SHIFT = 8
ALU_REG_CMD_START_SHIFT = 5
ALU_REG_CMD_DONE_SHIFT = 6


class TinyAluDriver:
    def __init__(self, poll_limit: int = 32, config: TargetConfig | None = None):
        self.bfm = TinyAluBfm()
        self.poll_limit = poll_limit
        self.config = config

    async def reset(self) -> None:
        await self.bfm.reset()

    async def execute(self, case: FuzzCase) -> ReplayResult:
        a = int(case.data["a"])
        b = int(case.data["b"])
        op = Ops(int(case.data["op"]))

        await self.bfm.SW_WRITE(ALU_REG_CMD_ADDR, 0)
        await self.bfm.SW_WRITE(ALU_REG_SRC_ADDR, (b << ALU_REG_SRC_DATA1_SHIFT) | a)
        await self.bfm.SW_WRITE(ALU_REG_CMD_ADDR, (1 << ALU_REG_CMD_START_SHIFT) | int(op))

        for _ in range(self.poll_limit):
            cmd = await self.bfm.SW_READ(ALU_REG_CMD_ADDR)
            if ((cmd >> ALU_REG_CMD_DONE_SHIFT) & 1) == 1:
                actual = await self.bfm.SW_READ(ALU_REG_RESULT_ADDR)
                expected = alu_prediction(a, b, op)
                if actual != expected:
                    raise AssertionError(
                        f"TinyALU mismatch A=0x{a:02x} B=0x{b:02x} op={op.name}: "
                        f"actual=0x{actual:04x} expected=0x{expected:04x}"
                    )
                return ReplayResult(
                    actual=f"0x{actual:04x}",
                    expected=f"0x{expected:04x}",
                    detail=f"A=0x{a:02x} B=0x{b:02x} op={op.name}",
                )

        raise TimeoutError(f"TinyALU operation did not finish: A=0x{a:02x} B=0x{b:02x} op={op.name}")
