from __future__ import annotations

from pyuvm import uvm_sequence

from fuzz_bfm.corpus import FuzzCase
from fuzz_pipeline.harness import connector_from_env, replay_case_metadata
from fuzz_uvm.transactions import FuzzSeqItem


class CorpusReplaySequence(uvm_sequence):
    def __init__(self, name: str, cases: list[FuzzCase]):
        super().__init__(name)
        self.cases = cases

    async def body(self) -> None:
        connector = connector_from_env("case_to_replay_driver", "sequencer", "replay_driver")
        for index, case in enumerate(self.cases):
            item = FuzzSeqItem(f"case_{index}", case, index)
            await connector.run_async(
                self._send_item,
                item,
                metadata=replay_case_metadata(case, index=index),
                metrics=lambda _value: {"sent": True},
            )

    async def _send_item(self, item: FuzzSeqItem) -> None:
        await self.start_item(item)
        await self.finish_item(item)
