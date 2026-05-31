from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fuzz_bfm.corpus import FuzzCase
from fuzz_bfm.plugin_loader import build_plugin
from fuzz_bfm.target_config import TargetConfig


@dataclass(frozen=True)
class ExpectedResult:
    expected: str
    detail: str = ""


class ReferenceModelProtocol(Protocol):
    def predict(self, case: FuzzCase) -> ExpectedResult:
        ...


def build_ref_model(config: TargetConfig) -> ReferenceModelProtocol | None:
    spec = config.ref_model or config.oracle
    if spec is None:
        return None
    return build_plugin(spec, target=config.name, config=config)
