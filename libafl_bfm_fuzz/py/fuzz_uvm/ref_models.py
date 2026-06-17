from __future__ import annotations

from fuzz_bfm.plugin_loader import build_plugin
from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.contracts import (
    ExpectedResult,
    ReferenceModelPlugin,
    validate_ref_model_plugin,
)


def build_ref_model(config: TargetConfig) -> ReferenceModelPlugin | None:
    spec = config.ref_model or config.oracle
    if spec is None:
        return None
    plugin = build_plugin(spec, target=config.name, config=config)
    return validate_ref_model_plugin(plugin, spec=spec)


__all__ = [
    "ExpectedResult",
    "ReferenceModelPlugin",
    "build_ref_model",
]
