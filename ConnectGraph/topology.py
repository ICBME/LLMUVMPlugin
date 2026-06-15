from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class ComponentNode:
    name: str
    kind: str
    description: str = ""
    metadata: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        if value["metadata"] is None:
            value.pop("metadata")
        return value


@dataclass(frozen=True)
class ConnectorEdge:
    name: str
    from_layer: str
    to_layer: str
    description: str = ""
    input_roles: tuple[str, ...] = ()
    output_roles: tuple[str, ...] = ()
    required: bool = True
    metadata: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        if value["metadata"] is None:
            value.pop("metadata")
        if not value["input_roles"]:
            value.pop("input_roles")
        if not value["output_roles"]:
            value.pop("output_roles")
        if value["required"]:
            value.pop("required")
        return value


@dataclass(frozen=True)
class PipelineTopology:
    name: str
    components: tuple[ComponentNode, ...]
    connectors: tuple[ConnectorEdge, ...]
    schema_version: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "components": [component.to_json() for component in self.components],
            "connectors": [connector.to_json() for connector in self.connectors],
        }


def merge_topologies(name: str, *topologies: PipelineTopology) -> PipelineTopology:
    components: dict[str, ComponentNode] = {}
    connectors: dict[str, ConnectorEdge] = {}
    schema_version = 1
    for topology in topologies:
        schema_version = max(schema_version, topology.schema_version)
        for component in topology.components:
            components.setdefault(component.name, component)
        for connector in topology.connectors:
            existing = connectors.get(connector.name)
            if existing is not None and (
                existing.from_layer != connector.from_layer
                or existing.to_layer != connector.to_layer
            ):
                raise ValueError(
                    f"connector {connector.name!r} has inconsistent topology edges: "
                    f"{existing.from_layer}->{existing.to_layer} vs "
                    f"{connector.from_layer}->{connector.to_layer}"
                )
            connectors.setdefault(connector.name, connector)
    return PipelineTopology(
        name=name,
        components=tuple(components.values()),
        connectors=tuple(connectors.values()),
        schema_version=schema_version,
    )


def write_topology(topology: PipelineTopology, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(topology.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_connector_endpoint(
    topology: PipelineTopology,
    connector_name: str,
    from_layer: str,
    to_layer: str,
) -> ConnectorEdge:
    for edge in topology.connectors:
        if edge.name != connector_name:
            continue
        if edge.from_layer != from_layer or edge.to_layer != to_layer:
            raise ValueError(
                f"connector {connector_name!r} endpoint mismatch: "
                f"expected {edge.from_layer}->{edge.to_layer}, got {from_layer}->{to_layer}"
            )
        return edge
    raise ValueError(f"unknown connector: {connector_name}")


def topology_out_from_env(env_var: str = "CONNECTOR_TOPOLOGY_OUT") -> Path | None:
    path = os.getenv(env_var)
    return Path(path) if path and path.strip() else None


def write_topology_from_env(
    topology: PipelineTopology,
    path: str | Path | None = None,
    *,
    env_var: str = "CONNECTOR_TOPOLOGY_OUT",
) -> Path | None:
    if path is not None:
        if not str(path).strip():
            return None
        output_path = path
    else:
        output_path = topology_out_from_env(env_var)
    if output_path is None:
        return None
    resolved = Path(output_path)
    write_topology(topology, resolved)
    return resolved
