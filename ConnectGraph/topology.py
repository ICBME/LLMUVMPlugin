from __future__ import annotations

from dataclasses import asdict, dataclass
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
