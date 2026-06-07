from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


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
    metadata: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        if value["metadata"] is None:
            value.pop("metadata")
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


COVERAGE_FEEDBACK_TOPOLOGY = PipelineTopology(
    name="coverage_feedback",
    components=(
        ComponentNode("coverage_artifacts", "artifact_source", "RTL and UVM coverage artifacts"),
        ComponentNode("coverage_summary", "analysis", "Normalized coverage and gap summary"),
        ComponentNode("mutation_feedback", "feedback", "Layer 3 mutation direction feedback"),
        ComponentNode("gap_feedback", "feedback", "Layer 2 per-gap feedback"),
        ComponentNode("layer1_plan", "planner", "Layer 1 RTL gap mutation plan"),
        ComponentNode("mutation_directives", "planner_output", "Next-round mutation directives"),
        ComponentNode("llm_prompt", "prompt", "Optional LLM prompt payload"),
        ComponentNode("llm_response", "llm_output", "Optional LLM response payload"),
    ),
    connectors=(
        ConnectorEdge("coverage_to_summary", "coverage_artifacts", "coverage_summary"),
        ConnectorEdge("summary_to_mutation_feedback", "coverage_summary", "mutation_feedback"),
        ConnectorEdge("summary_to_gap_feedback", "coverage_summary", "gap_feedback"),
        ConnectorEdge(
            "layer3_feedback_to_layer2_feedback",
            "mutation_feedback",
            "gap_feedback",
            "Layer 2 consumes Layer 3 direction decisions while classifying per-gap progress.",
        ),
        ConnectorEdge(
            "layer2_layer3_feedback_to_layer1_plan",
            "gap_feedback",
            "layer1_plan",
            "Layer 1 planner consumes Layer 2 gap state and Layer 3 direction state.",
        ),
        ConnectorEdge("layer1_plan_to_directives", "layer1_plan", "mutation_directives"),
        ConnectorEdge("summary_to_llm_prompt", "mutation_directives", "llm_prompt"),
        ConnectorEdge("llm_prompt_to_response", "llm_prompt", "llm_response"),
        ConnectorEdge("llm_response_to_directives", "llm_response", "mutation_directives"),
    ),
)
