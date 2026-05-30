from __future__ import annotations

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge


class MemoryMappedBfm:
    """BFM for secworks-style cs/we/address/write_data/read_data wrappers."""

    def __init__(self, poll_limit: int = 512):
        self.dut = cocotb.top
        self.poll_limit = poll_limit

    async def wait_clock(self, cycles: int = 1) -> None:
        for _ in range(cycles):
            await RisingEdge(self.dut.clk)

    async def reset(self) -> None:
        self.dut.cs.value = 0
        self.dut.we.value = 0
        self.dut.address.value = 0
        self.dut.write_data.value = 0
        self.dut.reset_n.value = 0
        await self.wait_clock(2)
        self.dut.reset_n.value = 1
        await self.wait_clock(2)

    async def write_word(self, address: int, word: int) -> None:
        await FallingEdge(self.dut.clk)
        self.dut.address.value = address
        self.dut.write_data.value = word & 0xFFFFFFFF
        self.dut.cs.value = 1
        self.dut.we.value = 1
        await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)
        self.dut.cs.value = 0
        self.dut.we.value = 0

    async def read_word(self, address: int) -> int:
        await FallingEdge(self.dut.clk)
        self.dut.address.value = address
        self.dut.cs.value = 1
        self.dut.we.value = 0
        await RisingEdge(self.dut.clk)
        await FallingEdge(self.dut.clk)
        value = int(self.dut.read_data.value) & 0xFFFFFFFF
        self.dut.cs.value = 0
        return value

    async def wait_status_bit(self, status_addr: int, bit: int, label: str) -> None:
        for _ in range(self.poll_limit):
            status = await self.read_word(status_addr)
            if ((status >> bit) & 1) == 1:
                return
        raise TimeoutError(f"timed out waiting for {label} at status bit {bit}")

