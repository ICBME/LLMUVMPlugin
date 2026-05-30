from __future__ import annotations

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge


DEFAULT_SIGNALS = {
    "clk": "clk",
    "reset_n": "reset_n",
    "cs": "cs",
    "we": "we",
    "address": "address",
    "write_data": "write_data",
    "read_data": "read_data",
}


class MemoryMappedBfm:
    """BFM for secworks-style cs/we/address/write_data/read_data wrappers."""

    def __init__(self, poll_limit: int = 512, dut=None, signals: dict[str, str] | None = None):
        self.dut = dut or cocotb.top
        self.poll_limit = poll_limit
        self.signals = {**DEFAULT_SIGNALS, **(signals or {})}

    def signal(self, name: str):
        return getattr(self.dut, self.signals[name])

    async def wait_clock(self, cycles: int = 1) -> None:
        for _ in range(cycles):
            await RisingEdge(self.signal("clk"))

    async def reset(self) -> None:
        self.signal("cs").value = 0
        self.signal("we").value = 0
        self.signal("address").value = 0
        self.signal("write_data").value = 0
        self.signal("reset_n").value = 0
        await self.wait_clock(2)
        self.signal("reset_n").value = 1
        await self.wait_clock(2)

    async def write_word(self, address: int, word: int) -> None:
        await FallingEdge(self.signal("clk"))
        self.signal("address").value = address
        self.signal("write_data").value = word & 0xFFFFFFFF
        self.signal("cs").value = 1
        self.signal("we").value = 1
        await RisingEdge(self.signal("clk"))
        await FallingEdge(self.signal("clk"))
        self.signal("cs").value = 0
        self.signal("we").value = 0

    async def read_word(self, address: int) -> int:
        await FallingEdge(self.signal("clk"))
        self.signal("address").value = address
        self.signal("cs").value = 1
        self.signal("we").value = 0
        await RisingEdge(self.signal("clk"))
        await FallingEdge(self.signal("clk"))
        value = int(self.signal("read_data").value) & 0xFFFFFFFF
        self.signal("cs").value = 0
        return value

    async def wait_status_bit(self, status_addr: int, bit: int, label: str) -> None:
        for _ in range(self.poll_limit):
            status = await self.read_word(status_addr)
            if ((status >> bit) & 1) == 1:
                return
        raise TimeoutError(f"timed out waiting for {label} at status bit {bit}")
