from __future__ import annotations

from pyuvm import ConfigDB, uvm_env, uvm_sequencer

from fuzz_uvm.components import (
    FunctionalCoverageSubscriber,
    ReplayDriver,
    ReplayScoreboard,
)


class FuzzEnv(uvm_env):
    def build_phase(self) -> None:
        self.seqr = uvm_sequencer("seqr", self)
        self.driver = ReplayDriver("driver", self)
        self.scoreboard = ReplayScoreboard("scoreboard", self)
        self.functional_coverage = FunctionalCoverageSubscriber("functional_coverage", self)

    def connect_phase(self) -> None:
        self.driver.seq_item_port.connect(self.seqr.seq_item_export)
        self.driver.ap.connect(self.scoreboard.analysis_export)
        self.driver.ap.connect(self.functional_coverage.analysis_export)
        ConfigDB().set(None, "*", "FUZZ_SEQUENCER", self.seqr)
