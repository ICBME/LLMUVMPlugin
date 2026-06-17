from __future__ import annotations

from collections import Counter
import hashlib
from typing import Any

from fuzz_bfm.bfm_base import ReplayResult
from fuzz_uvm.contracts import ExpectedResult


ADDR_CTRL = 0x08
ADDR_BLOCK0 = 0x10
ADDR_DIGEST0 = 0x20

CTRL_INIT_VALUE = 0x01
CTRL_NEXT_VALUE = 0x02
CTRL_MODE_VALUE = 0x04


class Sha256MmioDriver:
    def __init__(self, config=None):
        self.config = config

    async def reset(self) -> None:
        import cocotb
        from cocotb.triggers import RisingEdge

        self.dut = cocotb.top
        self.dut.cs.value = 0
        self.dut.we.value = 0
        self.dut.address.value = 0
        self.dut.write_data.value = 0
        self.dut.reset_n.value = 0
        for _ in range(3):
            await RisingEdge(self.dut.clk)
        self.dut.reset_n.value = 1
        for _ in range(3):
            await RisingEdge(self.dut.clk)

    async def execute(self, case) -> ReplayResult:
        data = case.data
        mode = str(data["mode"])
        message = bytes.fromhex(str(data["message"]))
        blocks = padded_sha_blocks(message)
        for index, block in enumerate(blocks):
            await self._write_words(ADDR_BLOCK0, _bytes_to_words(block))
            control = CTRL_INIT_VALUE if index == 0 else CTRL_NEXT_VALUE
            if mode == "sha256":
                control |= CTRL_MODE_VALUE
            await self._write_word(ADDR_CTRL, control)
            await self._wait_cycles(90)

        digest_words = await self._read_words(ADDR_DIGEST0, 8)
        digest = _words_to_bytes(digest_words).hex()
        actual = digest[:56] if mode == "sha224" else digest
        return ReplayResult(
            actual=actual,
            detail=f"{mode} message_len={len(message)} line={case.line_no}",
        )

    async def _write_words(self, base: int, words: list[int]) -> None:
        for offset, word in enumerate(words):
            await self._write_word(base + offset, word)

    async def _read_words(self, base: int, count: int) -> list[int]:
        return [await self._read_word(base + offset) for offset in range(count)]

    async def _write_word(self, address: int, word: int) -> None:
        from cocotb.triggers import RisingEdge

        self.dut.address.value = address
        self.dut.write_data.value = word & 0xFFFFFFFF
        self.dut.cs.value = 1
        self.dut.we.value = 1
        await RisingEdge(self.dut.clk)
        self.dut.cs.value = 0
        self.dut.we.value = 0
        await RisingEdge(self.dut.clk)

    async def _read_word(self, address: int) -> int:
        from cocotb.triggers import RisingEdge, Timer

        self.dut.address.value = address
        self.dut.cs.value = 1
        self.dut.we.value = 0
        await Timer(1, unit="step")
        value = int(self.dut.read_data.value)
        await RisingEdge(self.dut.clk)
        self.dut.cs.value = 0
        await RisingEdge(self.dut.clk)
        return value

    async def _wait_cycles(self, cycles: int) -> None:
        from cocotb.triggers import RisingEdge

        for _ in range(cycles):
            await RisingEdge(self.dut.clk)


class Sha256RefModel:
    def __init__(self, target=None, config=None):
        self.target = target
        self.config = config

    def predict(self, case) -> ExpectedResult:
        mode = str(case.data["mode"])
        message = bytes.fromhex(str(case.data["message"]))
        if mode == "sha224":
            expected = hashlib.sha224(message).hexdigest()
        elif mode == "sha256":
            expected = hashlib.sha256(message).hexdigest()
        else:
            raise ValueError(f"unsupported SHA mode={mode!r}")
        return ExpectedResult(expected=expected, detail=f"{mode}_ref len={len(message)}")


class Sha256CoverageModel:
    def __init__(self, target=None, config=None):
        self.target = target or "secworks_sha256"
        self.config = config
        self.total_cases = 0
        self.bins: dict[str, Counter[str]] = {}
        self.crosses: dict[str, Counter[str]] = {}
        self.origins: Counter[str] = Counter()

    def sample(self, case) -> None:
        if case.target != self.target:
            return
        self.total_cases += 1
        data = case.data
        message = bytes.fromhex(str(data["message"]))
        self.origins[str(data.get("origin", "unknown"))] += 1
        self._hit("mode", str(data["mode"]))
        self._hit("message_pattern", _byte_pattern(message))
        self._hit("message_length", _length_bucket(len(message)))
        self._cross("mode_x_message_length", str(data["mode"]), _length_bucket(len(message)))

    def sample_record(self, record) -> None:
        if record.error is None:
            self.sample(record.case)
        else:
            self._hit("record_error", record.error.split(":", 1)[0])

    def to_json(self) -> dict[str, Any]:
        uncovered: dict[str, dict[str, list[str]]] = {
            "fields": {
                "mode": _missing(self.bins.get("mode", Counter()), ("sha224", "sha256")),
            },
            "coverpoints": {
                "message_pattern": _missing(
                    self.bins.get("message_pattern", Counter()),
                    ("empty", "zero", "ff", "increment", "mixed"),
                ),
                "message_length": _missing(
                    self.bins.get("message_length", Counter()),
                    ("0", "1..15", "16..31", "32..55", "56+"),
                ),
            },
        }
        uncovered["fields"] = {key: value for key, value in uncovered["fields"].items() if value}
        uncovered["coverpoints"] = {
            key: value for key, value in uncovered["coverpoints"].items() if value
        }
        return {
            "domain": "uvm_functional",
            "target": self.target,
            "total_cases": self.total_cases,
            "origin_counts": dict(sorted(self.origins.items())),
            "bins": {key: dict(sorted(value.items())) for key, value in sorted(self.bins.items())},
            "crosses": {key: dict(sorted(value.items())) for key, value in sorted(self.crosses.items())},
            "coverage": {},
            "uncovered": {key: value for key, value in uncovered.items() if value},
        }

    def _hit(self, name: str, value: str) -> None:
        self.bins.setdefault(name, Counter())[value] += 1

    def _cross(self, name: str, *values: str) -> None:
        self.crosses.setdefault(name, Counter())["|".join(values)] += 1


def padded_sha_blocks(message: bytes) -> list[bytes]:
    bit_len = len(message) * 8
    padded = bytearray(message)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0)
    padded.extend(bit_len.to_bytes(8, "big"))
    return [bytes(padded[idx : idx + 64]) for idx in range(0, len(padded), 64)]


def _bytes_to_words(data: bytes) -> list[int]:
    return [int.from_bytes(data[idx : idx + 4], "big") for idx in range(0, len(data), 4)]


def _words_to_bytes(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(4, "big") for word in words)


def _byte_pattern(data: bytes) -> str:
    if not data:
        return "empty"
    if all(byte == 0 for byte in data):
        return "zero"
    if all(byte == 0xFF for byte in data):
        return "ff"
    if data == bytes(range(len(data))):
        return "increment"
    return "mixed"


def _length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length <= 15:
        return "1..15"
    if length <= 31:
        return "16..31"
    if length <= 55:
        return "32..55"
    return "56+"


def _missing(counter: Counter[str], expected: tuple[str, ...]) -> list[str]:
    return [value for value in expected if counter.get(value, 0) == 0]
