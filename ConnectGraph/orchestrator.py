"""Compatibility facade for shared orchestration primitives.

New code should prefer ``harness_optimization.orchestrator`` directly.
"""

from harness_optimization.orchestrator import (  # noqa: F401
    PipelineContext,
    PipelineOrchestrator,
    StepPolicy,
    StepSpec,
    external_command_step,
    validate_topology,
)

__all__ = [
    "PipelineContext",
    "PipelineOrchestrator",
    "StepPolicy",
    "StepSpec",
    "external_command_step",
    "validate_topology",
]
