"""Compatibility facade for shared run stage registry primitives.

New code should prefer ``harness_optimization.planning`` directly unless it
requires the historical ``fuzz_pipeline.run_stage_registry`` import path.
"""

from harness_optimization.planning import (  # noqa: F401
    RegisteredRunStage,
    RunStageFactory,
    RunStageRegistry,
)

__all__ = ["RegisteredRunStage", "RunStageFactory", "RunStageRegistry"]
