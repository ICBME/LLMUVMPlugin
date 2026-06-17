"""Compatibility facade over the shared harness_optimization orchestrator.

New code should prefer ``harness_optimization.orchestrator`` when it does not
need the default ``FULL_FUZZ_TOPOLOGY`` binding supplied here.
"""

from __future__ import annotations

from harness_optimization.orchestrator import (
    PipelineContext,
    PipelineOrchestrator as _SharedPipelineOrchestrator,
    StepPolicy,
    StepSpec,
    external_command_step,
    validate_topology,
)

from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


class PipelineOrchestrator(_SharedPipelineOrchestrator):
    def __init__(
        self,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
        observation_context=None,
        *,
        topology_out=None,
    ):
        super().__init__(
            topology,
            observation_context,
            topology_out=topology_out,
        )


__all__ = [
    "PipelineContext",
    "PipelineOrchestrator",
    "StepPolicy",
    "StepSpec",
    "external_command_step",
    "validate_topology",
]
