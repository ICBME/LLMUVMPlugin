"""Connector observation and topology primitives.

Package-root exports are intentionally limited to graph facts, connector
observation, topology helpers, and trace-quality utilities. Historical
orchestration imports remain available from ``ConnectGraph.orchestrator`` as a
compatibility facade over ``harness_optimization.orchestrator``.
"""

from .connector import Connector, ObservationContext
from .observation import (
    ObservationRuntime,
    close_observation,
    flush_observer,
    connector_from_env,
    observation_make_vars,
    observation_context_from_env,
)
from .observers import (
    AsyncObserver,
    CompositeObserver,
    JsonlObserver,
    MonitoringObserver,
    NullObserver,
    observer_from_env,
)
from .schema import ArtifactRef, ConnectorEvent, SCHEMA_VERSION
from .topology import (
    ComponentNode,
    ConnectorEdge,
    PipelineTopology,
    merge_topologies,
    topology_out_from_env,
    validate_connector_endpoint,
    write_topology_from_env,
    write_topology,
)
from .trace import (
    FINAL_EVENT_TYPES,
    STARTED_EVENT_TYPE,
    event_span_id,
    event_status,
    read_json_object,
    read_jsonl_events,
    trace_quality,
)

__all__ = [
    "AsyncObserver",
    "ArtifactRef",
    "close_observation",
    "ComponentNode",
    "CompositeObserver",
    "Connector",
    "ConnectorEdge",
    "ConnectorEvent",
    "FINAL_EVENT_TYPES",
    "JsonlObserver",
    "MonitoringObserver",
    "NullObserver",
    "ObservationContext",
    "ObservationRuntime",
    "SCHEMA_VERSION",
    "STARTED_EVENT_TYPE",
    "PipelineTopology",
    "event_span_id",
    "event_status",
    "flush_observer",
    "merge_topologies",
    "connector_from_env",
    "observation_context_from_env",
    "observation_make_vars",
    "observer_from_env",
    "read_json_object",
    "read_jsonl_events",
    "topology_out_from_env",
    "validate_connector_endpoint",
    "trace_quality",
    "write_topology_from_env",
    "write_topology",
]
