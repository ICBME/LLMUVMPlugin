from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from fuzz_bfm.bfm_base import ReplayResult
from fuzz_uvm.ref_models import ExpectedResult


ADDR_CTRL = 0x08
ADDR_STATUS = 0x09
ADDR_CONFIG = 0x0A
ADDR_KEY0 = 0x10
ADDR_BLOCK0 = 0x20
ADDR_RESULT0 = 0x30
AES_MMIO_REGISTERS = {
    "ADDR_NAME0": 0x00,
    "ADDR_NAME1": 0x01,
    "ADDR_VERSION": 0x02,
    "ADDR_CTRL": ADDR_CTRL,
    "ADDR_STATUS": ADDR_STATUS,
    "ADDR_CONFIG": ADDR_CONFIG,
    "ADDR_KEY0": ADDR_KEY0,
    "ADDR_KEY1": ADDR_KEY0 + 1,
    "ADDR_KEY2": ADDR_KEY0 + 2,
    "ADDR_KEY3": ADDR_KEY0 + 3,
    "ADDR_KEY4": ADDR_KEY0 + 4,
    "ADDR_KEY5": ADDR_KEY0 + 5,
    "ADDR_KEY6": ADDR_KEY0 + 6,
    "ADDR_KEY7": ADDR_KEY0 + 7,
    "ADDR_BLOCK0": ADDR_BLOCK0,
    "ADDR_BLOCK1": ADDR_BLOCK0 + 1,
    "ADDR_BLOCK2": ADDR_BLOCK0 + 2,
    "ADDR_BLOCK3": ADDR_BLOCK0 + 3,
    "ADDR_RESULT0": ADDR_RESULT0,
    "ADDR_RESULT1": ADDR_RESULT0 + 1,
    "ADDR_RESULT2": ADDR_RESULT0 + 2,
    "ADDR_RESULT3": ADDR_RESULT0 + 3,
}

AES_DECIPHER = 0
AES_ENCIPHER = 1
AES_128_BIT_KEY = 0
AES_256_BIT_KEY = 1


class AesMmioDriver:
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
        op = str(data["op"])
        key_len = int(data["key_len"])
        key = bytes.fromhex(str(data["key"]))
        block = bytes.fromhex(str(data["block"]))
        if key_len not in {128, 256}:
            raise ValueError(f"unsupported AES key_len={key_len}")
        if len(block) != 16:
            raise ValueError("AES block must be 16 bytes")
        if len(key) != key_len // 8:
            raise ValueError(f"AES-{key_len} key must be {key_len // 8} bytes")

        await self._init_key(key, key_len)
        await self._write_words(ADDR_BLOCK0, _bytes_to_words(block))
        encdec = AES_ENCIPHER if op == "encrypt" else AES_DECIPHER
        keylen_bit = AES_256_BIT_KEY if key_len == 256 else AES_128_BIT_KEY
        await self._write_word(ADDR_CONFIG, (keylen_bit << 1) | encdec)
        await self._write_word(ADDR_CTRL, 0x02)
        await self._wait_cycles(100)
        result = await self._read_words(ADDR_RESULT0, 4)
        actual = _words_to_bytes(result).hex()
        return ReplayResult(
            actual=actual,
            detail=f"aes {op} key_len={key_len} line={case.line_no}",
        )

    async def _init_key(self, key: bytes, key_len: int) -> None:
        padded = key + bytes(32 - len(key))
        await self._write_words(ADDR_KEY0, _bytes_to_words(padded))
        keylen_bit = AES_256_BIT_KEY if key_len == 256 else AES_128_BIT_KEY
        await self._write_word(ADDR_CONFIG, keylen_bit << 1)
        await self._write_word(ADDR_CTRL, 0x01)
        await self._wait_cycles(100)

    async def _write_words(self, base: int, words: list[int]) -> None:
        for offset, word in enumerate(words):
            await self._write_word(base + offset, word)

    async def _read_words(self, base: int, count: int) -> list[int]:
        return [await self._read_word(base + offset) for offset in range(count)]

    async def read_mmio_word(self, address: int) -> int:
        return await self._read_word(address)

    def resolve_mmio_address(self, name: str) -> int | None:
        return AES_MMIO_REGISTERS.get(str(name))

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


class AesRefModel:
    def __init__(self, target=None, config=None):
        self.target = target
        self.config = config

    def predict(self, case) -> ExpectedResult:
        data = case.data
        op = str(data["op"])
        key_len = int(data["key_len"])
        key = bytes.fromhex(str(data["key"]))
        block = bytes.fromhex(str(data["block"]))
        if op == "encrypt":
            expected = aes_encrypt_block(key, block).hex()
        elif op == "decrypt":
            expected = aes_decrypt_block(key, block).hex()
        else:
            raise ValueError(f"unsupported AES op={op!r}")
        return ExpectedResult(expected=expected, detail=f"aes_ref {op} key_len={key_len}")


class AesCoverageModel:
    def __init__(self, target=None, config=None):
        self.target = target or "secworks_aes"
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
        self.origins[str(data.get("origin", "unknown"))] += 1
        self._hit("op", str(data["op"]))
        self._hit("key_len", str(data["key_len"]))
        self._hit("block_pattern", _byte_pattern(bytes.fromhex(str(data["block"]))))
        self._hit("key_pattern", _byte_pattern(bytes.fromhex(str(data["key"]))))
        self._cross("op_x_key_len", str(data["op"]), str(data["key_len"]))

    def sample_record(self, record) -> None:
        if record.error is None:
            self.sample(record.case)
        else:
            self._hit("record_error", record.error.split(":", 1)[0])

    def to_json(self) -> dict[str, Any]:
        uncovered: dict[str, dict[str, list[str]]] = {
            "fields": {
                "op": _missing(self.bins.get("op", Counter()), ("encrypt", "decrypt")),
                "key_len": _missing(self.bins.get("key_len", Counter()), ("128", "256")),
            },
            "coverpoints": {
                "block_pattern": _missing(
                    self.bins.get("block_pattern", Counter()),
                    ("zero", "ff", "increment", "mixed"),
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


def aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    round_keys = _expand_key(key)
    state = list(block)
    _add_round_key(state, round_keys[0])
    for round_key in round_keys[1:-1]:
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_key)
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, round_keys[-1])
    return bytes(state)


def aes_decrypt_block(key: bytes, block: bytes) -> bytes:
    round_keys = _expand_key(key)
    state = list(block)
    _add_round_key(state, round_keys[-1])
    for round_key in reversed(round_keys[1:-1]):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, round_key)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, round_keys[0])
    return bytes(state)


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) not in {16, 32}:
        raise ValueError("AES key must be 16 or 32 bytes")
    nk = len(key) // 4
    nr = nk + 6
    words = [list(key[idx : idx + 4]) for idx in range(0, len(key), 4)]
    for idx in range(nk, 4 * (nr + 1)):
        temp = words[idx - 1].copy()
        if idx % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[value] for value in temp]
            temp[0] ^= _RCON[idx // nk]
        elif nk > 6 and idx % nk == 4:
            temp = [_SBOX[value] for value in temp]
        words.append([left ^ right for left, right in zip(words[idx - nk], temp)])
    return [sum(words[round_idx * 4 : (round_idx + 1) * 4], []) for round_idx in range(nr + 1)]


def _add_round_key(state: list[int], round_key: list[int]) -> None:
    for idx, value in enumerate(round_key):
        state[idx] ^= value


def _sub_bytes(state: list[int]) -> None:
    for idx, value in enumerate(state):
        state[idx] = _SBOX[value]


def _inv_sub_bytes(state: list[int]) -> None:
    for idx, value in enumerate(state):
        state[idx] = _INV_SBOX[value]


def _shift_rows(state: list[int]) -> None:
    for row in range(1, 4):
        values = [state[col * 4 + row] for col in range(4)]
        for col in range(4):
            state[col * 4 + row] = values[(col + row) % 4]


def _inv_shift_rows(state: list[int]) -> None:
    for row in range(1, 4):
        values = [state[col * 4 + row] for col in range(4)]
        for col in range(4):
            state[col * 4 + row] = values[(col - row) % 4]


def _mix_columns(state: list[int]) -> None:
    for col in range(4):
        idx = col * 4
        a0, a1, a2, a3 = state[idx : idx + 4]
        state[idx + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        state[idx + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        state[idx + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        state[idx + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)


def _inv_mix_columns(state: list[int]) -> None:
    for col in range(4):
        idx = col * 4
        a0, a1, a2, a3 = state[idx : idx + 4]
        state[idx + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        state[idx + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        state[idx + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        state[idx + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def _gmul(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        high = left & 0x80
        left = (left << 1) & 0xFF
        if high:
            left ^= 0x1B
        right >>= 1
    return result


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


def _missing(counter: Counter[str], expected: tuple[str, ...]) -> list[str]:
    return [value for value in expected if counter.get(value, 0) == 0]


_SBOX = [
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
]

_INV_SBOX = [0] * 256
for _idx, _value in enumerate(_SBOX):
    _INV_SBOX[_value] = _idx

_RCON = [
    0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
    0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D, 0x9A,
]
