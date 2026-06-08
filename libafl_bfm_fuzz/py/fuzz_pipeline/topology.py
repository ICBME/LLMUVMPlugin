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


HARNESS_TOPOLOGY = PipelineTopology(
    name="harness_execution",
    components=(
        ComponentNode("target_manifest", "config", "Target manifest and plugin references"),
        ComponentNode("mutation_directives", "planner_output", "Mutation directives from feedback"),
        ComponentNode("corpus_generator", "fuzzer", "LibAFL schema-driven corpus generator"),
        ComponentNode("corpus", "artifact", "Generated JSONL replay corpus"),
        ComponentNode("corpus_validator", "validator", "Manifest-schema corpus validator"),
        ComponentNode("replay_context", "replay_input", "Loaded replay target, config, and cases"),
        ComponentNode("sequencer", "uvm_component", "pyUVM corpus replay sequence"),
        ComponentNode("replay_driver", "uvm_component", "Target replay driver BFM"),
        ComponentNode("ref_model", "oracle", "Reference model prediction"),
        ComponentNode("dut", "rtl", "DUT execution under replay"),
        ComponentNode("scoreboard", "checker", "Replay result checker"),
        ComponentNode("functional_coverage", "coverage", "UVM functional coverage model"),
        ComponentNode("scoreboard_report", "artifact", "Replay scoreboard health summary"),
        ComponentNode("functional_coverage_summary", "artifact", "Exported functional coverage JSON"),
    ),
    connectors=(
        ConnectorEdge("manifest_to_corpus_generator", "target_manifest", "corpus_generator", input_roles=("target_manifest",)),
        ConnectorEdge("directives_to_corpus_generator", "mutation_directives", "corpus_generator", input_roles=("directives",)),
        ConnectorEdge("corpus_generator_to_corpus", "corpus_generator", "corpus", output_roles=("corpus",)),
        ConnectorEdge("corpus_to_validation", "corpus", "corpus_validator", input_roles=("corpus",), output_roles=("corpus",)),
        ConnectorEdge("corpus_to_replay_context", "corpus", "replay_context", input_roles=("corpus",), output_roles=("corpus",)),
        ConnectorEdge("manifest_to_replay_driver", "target_manifest", "replay_driver", input_roles=("target_manifest",)),
        ConnectorEdge("manifest_to_ref_model", "target_manifest", "ref_model", input_roles=("target_manifest",)),
        ConnectorEdge("replay_context_to_sequence", "replay_context", "sequencer", input_roles=("corpus",)),
        ConnectorEdge("case_to_replay_driver", "sequencer", "replay_driver"),
        ConnectorEdge("driver_reset_to_dut", "replay_driver", "dut"),
        ConnectorEdge("case_to_dut", "replay_driver", "dut"),
        ConnectorEdge("case_to_ref_model", "replay_driver", "ref_model"),
        ConnectorEdge("driver_to_scoreboard", "replay_driver", "scoreboard"),
        ConnectorEdge("driver_to_functional_coverage", "replay_driver", "functional_coverage"),
        ConnectorEdge("scoreboard_to_report", "scoreboard", "scoreboard_report"),
        ConnectorEdge(
            "functional_coverage_to_summary",
            "functional_coverage",
            "functional_coverage_summary",
            output_roles=("functional_coverage",),
        ),
    ),
)


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
        ConnectorEdge(
            "coverage_to_summary",
            "coverage_artifacts",
            "coverage_summary",
            input_roles=("coverage_info", "corpus"),
            output_roles=("summary",),
        ),
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
        ConnectorEdge("layer1_plan_to_directives", "layer1_plan", "mutation_directives", output_roles=("directives",)),
        ConnectorEdge("summary_to_llm_prompt", "mutation_directives", "llm_prompt", output_roles=("prompt",)),
        ConnectorEdge("llm_prompt_to_response", "llm_prompt", "llm_response"),
        ConnectorEdge("llm_response_to_directives", "llm_response", "mutation_directives", output_roles=("directives",)),
    ),
)


FULL_FUZZ_TOPOLOGY = merge_topologies(
    "libafl_bfm_fuzz",
    HARNESS_TOPOLOGY,
    COVERAGE_FEEDBACK_TOPOLOGY,
)
