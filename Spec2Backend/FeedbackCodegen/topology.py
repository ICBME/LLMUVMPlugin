"""ConnectGraph topology for feedback-driven code generation."""

from __future__ import annotations

from ConnectGraph.topology import ComponentNode, ConnectorEdge, PipelineTopology


FEEDBACK_CODEGEN_TOPOLOGY = PipelineTopology(
    name="spec2backend_feedback_codegen",
    components=(
        ComponentNode("ref_model_plan", "plan", "Structured backend plan consumed by codegen"),
        ComponentNode("llm_prompt", "prompt", "Strict JSON prompt for code generation"),
        ComponentNode("llm_response", "llm_output", "Raw LLM response payload"),
        ComponentNode("candidate_bundle", "artifact", "Normalized generated file bundle"),
        ComponentNode("candidate_artifacts", "artifact_dir", "Generated candidate source files"),
        ComponentNode("candidate_evaluation", "validator", "Static, contract, and golden-case checks"),
        ComponentNode("feedback", "feedback", "Structured blocking issues for the next attempt"),
        ComponentNode("final_artifact", "artifact_dir", "Promoted generated files"),
    ),
    connectors=(
        ConnectorEdge(
            "plan_to_prompt",
            "ref_model_plan",
            "llm_prompt",
            input_roles=("plan",),
            output_roles=("prompt",),
        ),
        ConnectorEdge(
            "prompt_to_llm_response",
            "llm_prompt",
            "llm_response",
            input_roles=("prompt",),
            output_roles=("response",),
        ),
        ConnectorEdge(
            "response_to_candidate",
            "llm_response",
            "candidate_bundle",
            input_roles=("response",),
            output_roles=("candidate_bundle", "candidate_artifacts"),
        ),
        ConnectorEdge(
            "candidate_to_evaluation",
            "candidate_artifacts",
            "candidate_evaluation",
            input_roles=("candidate_bundle", "candidate_artifacts"),
            output_roles=("evaluation",),
        ),
        ConnectorEdge(
            "evaluation_to_feedback",
            "candidate_evaluation",
            "feedback",
            input_roles=("evaluation",),
            output_roles=("feedback",),
        ),
        ConnectorEdge(
            "candidate_to_final_artifact",
            "candidate_artifacts",
            "final_artifact",
            input_roles=("candidate_bundle", "candidate_artifacts", "evaluation"),
            output_roles=("final_artifact",),
        ),
    ),
)


__all__ = ["FEEDBACK_CODEGEN_TOPOLOGY"]
