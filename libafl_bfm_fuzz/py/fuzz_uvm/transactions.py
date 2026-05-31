from __future__ import annotations

from dataclasses import dataclass

from pyuvm import uvm_sequence_item

from fuzz_bfm.bfm_base import ReplayResult
from fuzz_bfm.corpus import FuzzCase


@dataclass(frozen=True)
class ReplayRecord:
    index: int
    case: FuzzCase
    result: ReplayResult | None = None
    error: str | None = None


class FuzzSeqItem(uvm_sequence_item):
    def __init__(self, name: str, case: FuzzCase, index: int):
        super().__init__(name)
        self.case = case
        self.index = index
        self.result: ReplayResult | None = None
        self.error: str | None = None

    def __str__(self) -> str:
        origin = self.case.data.get("origin", "libafl")
        return f"{self.get_name()} target={self.case.target} line={self.case.line_no} origin={origin}"
