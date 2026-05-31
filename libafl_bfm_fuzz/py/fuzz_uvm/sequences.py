from __future__ import annotations

from pyuvm import uvm_sequence

from fuzz_bfm.corpus import FuzzCase
from fuzz_uvm.transactions import FuzzSeqItem


class CorpusReplaySequence(uvm_sequence):
    def __init__(self, name: str, cases: list[FuzzCase]):
        super().__init__(name)
        self.cases = cases

    async def body(self) -> None:
        for index, case in enumerate(self.cases):
            item = FuzzSeqItem(f"case_{index}", case, index)
            await self.start_item(item)
            await self.finish_item(item)
