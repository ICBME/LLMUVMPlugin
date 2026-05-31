from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from fuzz_bfm.corpus import FuzzCase, load_cases
from fuzz_bfm.target_config import TargetConfig, load_target_config


@dataclass(frozen=True)
class ReplayContext:
    target: str
    config: TargetConfig
    corpus: Path
    cases: list[FuzzCase]

    @classmethod
    def from_env(cls) -> ReplayContext:
        target = os.getenv("FUZZ_TARGET", "tinyalu")
        config = load_target_config(target)
        corpus = Path(os.getenv("LIBAFL_CORPUS", f"coverage/{target}_corpus.jsonl"))
        cases = load_cases(corpus, target, config=config)
        return cls(target=target, config=config, corpus=corpus, cases=cases)
