from __future__ import annotations

from .bfm_base import ReplayResult
from .corpus import FuzzCase, bytes_to_words, hex_to_bytes, words_to_bytes
from .mem_bus_bfm import MemoryMappedBfm
from .target_config import TargetConfig


ADDR_CTRL = 0x08
ADDR_STATUS = 0x09
ADDR_CONFIG = 0x0A
ADDR_KEY0 = 0x10
ADDR_BLOCK0 = 0x20
ADDR_RESULT0 = 0x30
STATUS_READY_BIT = 0
STATUS_VALID_BIT = 1
CTRL_INIT = 0x01
CTRL_NEXT = 0x02
AES_DECIPHER = 0
AES_ENCIPHER = 1
AES_128_BIT_KEY = 0
AES_256_BIT_KEY = 1


class AesDriver:
    def __init__(self, config: TargetConfig | None = None):
        self.bfm = MemoryMappedBfm(poll_limit=512, signals=config.signals if config else None)

    async def reset(self) -> None:
        await self.bfm.reset()

    async def execute(self, case: FuzzCase) -> ReplayResult:
        key_len = int(case.data["key_len"])
        key = hex_to_bytes(str(case.data["key"]))
        block = hex_to_bytes(str(case.data["block"]))
        encipher = case.data["encdec"] == "encipher"

        await self._init_key(key, key_len)
        await self._write_words(ADDR_BLOCK0, bytes_to_words(block))

        keylen_bit = AES_256_BIT_KEY if key_len == 256 else AES_128_BIT_KEY
        encdec_bit = AES_ENCIPHER if encipher else AES_DECIPHER
        await self.bfm.write_word(ADDR_CONFIG, (keylen_bit << 1) | encdec_bit)
        await self.bfm.write_word(ADDR_CTRL, CTRL_NEXT)
        await self.bfm.wait_status_bit(ADDR_STATUS, STATUS_VALID_BIT, "aes valid")

        actual_words = [await self.bfm.read_word(ADDR_RESULT0 + idx) for idx in range(4)]
        actual = words_to_bytes(actual_words)
        return ReplayResult(
            actual=actual.hex(),
            detail=f"key_len={key_len} encdec={case.data['encdec']}",
        )

    async def _init_key(self, key: bytes, key_len: int) -> None:
        key_words = bytes_to_words(key)
        if key_len == 128:
            key_words = key_words + [0, 0, 0, 0]
        await self._write_words(ADDR_KEY0, key_words)
        keylen_bit = AES_256_BIT_KEY if key_len == 256 else AES_128_BIT_KEY
        await self.bfm.write_word(ADDR_CONFIG, keylen_bit << 1)
        await self.bfm.write_word(ADDR_CTRL, CTRL_INIT)
        await self.bfm.wait_status_bit(ADDR_STATUS, STATUS_READY_BIT, "aes ready after key init")

    async def _write_words(self, base_addr: int, words: list[int]) -> None:
        for idx, word in enumerate(words):
            await self.bfm.write_word(base_addr + idx, word)
