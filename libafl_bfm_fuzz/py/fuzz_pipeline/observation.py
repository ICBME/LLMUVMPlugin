"""Compatibility facade for observation helpers.

New code should prefer ``ConnectGraph.observation`` directly unless it needs
the fuzz_pipeline import path for backward compatibility.
"""

from ConnectGraph.observation import (  # noqa: F401
    ObservationRuntime,
    close_observation,
    flush_observer,
    connector_from_env,
    observation_make_vars,
    observation_context_from_env,
)

__all__ = [
    "ObservationRuntime",
    "close_observation",
    "flush_observer",
    "connector_from_env",
    "observation_make_vars",
    "observation_context_from_env",
]
