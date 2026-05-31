from __future__ import annotations

import hashlib

from .bfm_base import ReplayResult
from .corpus import FuzzCase, bytes_to_words, hex_to_bytes, words_to_bytes
from .mem_bus_bfm import MemoryMappedBfm
from .target_config import TargetConfig


ADDR_CTRL = 0x08
ADDR_STATUS = 0x09
ADDR_BLOCK0 = 0x10
ADDR_DIGEST0 = 0x20
STATUS_READY_BIT = 0
CTRL_INIT = 0x01
CTRL_NEXT = 0x02
CTRL_MODE = 0x04


class Sha256Driver:
    def __init__(self, config: TargetConfig | None = None):
        self.bfm = MemoryMappedBfm(poll_limit=1024, signals=config.signals if config else None)

    async def reset(self) -> None:
        await self.bfm.reset()

    async def execute(self, case: FuzzCase) -> ReplayResult:
        mode = str(case.data["mode"])
        message = hex_to_bytes(str(case.data["message"]))
        blocks = sha2_padded_blocks(message)

        for idx, block in enumerate(blocks):
            await self._write_block(block)
            ctrl = CTRL_INIT if idx == 0 else CTRL_NEXT
            if mode == "sha256":
                ctrl |= CTRL_MODE
            await self.bfm.write_word(ADDR_CTRL, ctrl)
            await self.bfm.wait_status_bit(ADDR_STATUS, STATUS_READY_BIT, "sha256 ready")

        digest_words = [await self.bfm.read_word(ADDR_DIGEST0 + idx) for idx in range(8)]
        actual = words_to_bytes(digest_words)
        expected = hashlib.sha256(message).digest() if mode == "sha256" else hashlib.sha224(message).digest()
        comparable = actual if mode == "sha256" else actual[:28]
        if comparable != expected:
            raise AssertionError(
                f"SHA mismatch mode={mode} len={len(message)}: "
                f"actual={comparable.hex()} expected={expected.hex()}"
            )
        return ReplayResult(
            actual=comparable.hex(),
            expected=expected.hex(),
            detail=f"mode={mode} len={len(message)} blocks={len(blocks)}",
        )

    async def _write_block(self, block: bytes) -> None:
        for idx, word in enumerate(bytes_to_words(block)):
            await self.bfm.write_word(ADDR_BLOCK0 + idx, word)


def sha2_padded_blocks(message: bytes) -> list[bytes]:
    bit_len = len(message) * 8
    padded = bytearray(message)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0)
    padded.extend(bit_len.to_bytes(8, "big"))
    return [bytes(padded[idx : idx + 64]) for idx in range(0, len(padded), 64)]
