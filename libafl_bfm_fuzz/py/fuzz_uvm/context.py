from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from fuzz_bfm.corpus import FuzzCase, load_cases
from fuzz_bfm.target_config import TargetConfig, load_target_config
from fuzz_pipeline.replay_orchestrator import (
    ReplayPipelineOrchestrator,
    replay_corpus_from_env,
)


@dataclass(frozen=True)
class ReplayContext:
    target: str
    config: TargetConfig
    corpus: Path
    cases: list[FuzzCase]

    @classmethod
    def from_env(cls) -> ReplayContext:
        corpus = replay_corpus_from_env()
        return ReplayPipelineOrchestrator.from_env(
            corpus=corpus,
        ).load_replay_context(
            cls._from_env,
            corpus=corpus,
        )

    @classmethod
    def _from_env(cls) -> ReplayContext:
        target = os.getenv("FUZZ_TARGET")
        if target is None and os.getenv("FUZZ_TARGET_CONFIG") is None:
            raise RuntimeError("FUZZ_TARGET or FUZZ_TARGET_CONFIG must be set")
        config = load_target_config(target or "dut")
        target = config.name
        corpus = Path(os.getenv("LIBAFL_CORPUS", f"coverage/{target}_corpus.jsonl"))
        cases = load_cases(corpus, target, config=config)
        return cls(target=target, config=config, corpus=corpus, cases=cases)
