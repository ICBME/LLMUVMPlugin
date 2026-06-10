from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fuzz_bfm.corpus import FuzzCase, load_cases
from fuzz_bfm.target_config import TargetConfig, load_target_config
from fuzz_pipeline.replay_orchestrator import (
    ReplayPipelineOrchestrator,
    replay_corpus_from_env,
    replay_target_from_env,
)


@dataclass(frozen=True)
class ReplayContext:
    target: str
    config: TargetConfig
    corpus: Path
    cases: list[FuzzCase]

    @classmethod
    def from_env(cls) -> ReplayContext:
        config = load_target_config(replay_target_from_env())
        corpus = replay_corpus_from_env(config.name)
        return ReplayPipelineOrchestrator.from_env(
            config=config,
            corpus=corpus,
        ).load_replay_context(
            lambda: cls._from_config(config, corpus),
            corpus=corpus,
        )

    @classmethod
    def _from_config(cls, config: TargetConfig, corpus: Path) -> ReplayContext:
        target = config.name
        cases = load_cases(corpus, target, config=config)
        return cls(target=target, config=config, corpus=corpus, cases=cases)
