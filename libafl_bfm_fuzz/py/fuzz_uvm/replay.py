from __future__ import annotations

from pyuvm import ConfigDB, uvm_test

from fuzz_uvm.components import (
    FunctionalCoverageSubscriber,
    ReplayDriver,
    ReplayScoreboard,
)
from fuzz_uvm.context import ReplayContext
from fuzz_uvm.env import FuzzEnv
from fuzz_uvm.sequences import CorpusReplaySequence
from fuzz_uvm.transactions import FuzzSeqItem, ReplayRecord
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator


class LibAflUvmReplayTest(uvm_test):
    def build_phase(self) -> None:
        self.context = ReplayContext.from_env()
        self.replay_orchestrator = ReplayPipelineOrchestrator.from_env(
            config=self.context.config,
            corpus=self.context.corpus,
        )
        ConfigDB().set(None, "*", "FUZZ_TARGET", self.context.target)
        ConfigDB().set(None, "*", "FUZZ_TARGET_CONFIG", self.context.config)
        ConfigDB().set(None, "*", "FUZZ_REPLAY_CONTEXT", self.context)
        ConfigDB().set(None, "*", "FUZZ_REPLAY_ORCHESTRATOR", self.replay_orchestrator)
        self.env = FuzzEnv("env", self)

    async def run_phase(self) -> None:
        import cocotb
        from cocotb.clock import Clock

        self.raise_objection()
        try:
            clock_signal = getattr(cocotb.top, self.context.config.clock)
            clock = Clock(clock_signal, self.context.config.clock_period_ns, "ns")
            cocotb.start_soon(clock.start())

            self.logger.info(
                "LibAFL UVM replay: target=%s driver=%s clock=%s corpus=%s total_cases=%d",
                self.context.target,
                self.context.config.driver,
                self.context.config.clock,
                self.context.corpus,
                len(self.context.cases),
            )
            sequence = CorpusReplaySequence(
                "corpus_replay",
                self.context.cases,
                self.replay_orchestrator,
            )
            await self.replay_orchestrator.start_sequence(
                lambda: sequence.start(self.env.seqr),
                corpus=self.context.corpus,
                target=self.context.target,
                case_count=len(self.context.cases),
            )
        finally:
            self.drop_objection()


__all__ = [
    "CorpusReplaySequence",
    "FunctionalCoverageSubscriber",
    "FuzzEnv",
    "FuzzSeqItem",
    "LibAflUvmReplayTest",
    "ReplayContext",
    "ReplayDriver",
    "ReplayRecord",
    "ReplayScoreboard",
]
