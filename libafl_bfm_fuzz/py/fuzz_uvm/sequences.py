from __future__ import annotations

from pyuvm import uvm_sequence

from fuzz_bfm.corpus import FuzzCase
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator
from fuzz_uvm.transactions import FuzzSeqItem


class CorpusReplaySequence(uvm_sequence):
    def __init__(
        self,
        name: str,
        cases: list[FuzzCase],
        orchestrator: ReplayPipelineOrchestrator,
    ):
        super().__init__(name)
        self.cases = cases
        self.orchestrator = orchestrator

    async def body(self) -> None:
        for index, case in enumerate(self.cases):
            item = FuzzSeqItem(f"case_{index}", case, index)
            await self.orchestrator.send_case(
                lambda item=item: self._send_item(item),
                case,
                index=index,
            )

    async def _send_item(self, item: FuzzSeqItem) -> None:
        await self.start_item(item)
        await self.finish_item(item)
