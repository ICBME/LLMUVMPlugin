from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from connector_observe import ObservationContext
from fuzz_feedback.advisors import (
    build_llm_prompt,
    maybe_call_llm,
    propose_directives_from_plan,
    validate_directives,
    write_llm_prompt,
)
from fuzz_feedback.coverage import build_summary
from fuzz_feedback.feedback_loop import build_gap_feedback, build_mutation_feedback
from fuzz_feedback.mutation_planner import plan_mutations_from_rtl_gaps

from .orchestrator import PipelineContext, PipelineOrchestrator, StepSpec
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


@dataclass(frozen=True)
class CoverageFeedbackConfig:
    target: str
    coverage_info: Path
    corpus: Path
    summary_out: Path
    directives_out: Path
    prompt_out: Path
    coverage_dat: Path | None = None
    functional_coverage: Path | None = None
    ignore_functional_coverage: bool = False
    previous_summary: Path | None = None
    previous_directives: Path | None = None
    previous_gap_feedback: Path | None = None
    previous_mutation_feedback: Path | None = None
    gap_feedback_out: Path | None = None
    mutation_feedback_out: Path | None = None
    llm: bool = False
    llm_response_out: Path | None = None
    model: str | None = None
    topology_out: Path | None = None


@dataclass(frozen=True)
class CoverageFeedbackResult:
    summary: dict[str, Any]
    final_directives: dict[str, Any]
    heuristic_directives: dict[str, Any]
    prompt: dict[str, Any]
    gap_feedback: dict[str, Any] | None = None
    mutation_feedback: dict[str, Any] | None = None


class CoverageFeedbackPipeline:
    def __init__(
        self,
        config: CoverageFeedbackConfig,
        context: ObservationContext,
        *,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
    ):
        self.config = config
        self.context = context
        self.topology = topology
        self.orchestrator = PipelineOrchestrator(
            topology,
            context,
            topology_out=config.topology_out,
        )

    def run(self) -> CoverageFeedbackResult:
        config = self.config
        pipeline_context = self._pipeline_context()
        self.orchestrator.write_topology()

        summary = self.orchestrator.run_step(
            StepSpec(
                name="summary",
                connector="coverage_to_summary",
                handler=lambda _context: build_summary(
                    config.target,
                    config.coverage_info,
                    config.corpus,
                    coverage_dat=config.coverage_dat,
                    functional_coverage=config.functional_coverage,
                    ignore_functional_coverage=config.ignore_functional_coverage,
                ),
                input_roles=self._summary_input_roles(),
                output_roles=("summary",),
                metrics=summary_metrics,
            ),
            pipeline_context,
        )

        previous_summary = read_optional_json(config.previous_summary)
        previous_directives = read_optional_json(config.previous_directives)
        previous_gap_feedback = read_optional_json(config.previous_gap_feedback)
        previous_mutation_feedback = read_optional_json(config.previous_mutation_feedback)
        applied_directives = previous_directives or {"directives": []}

        mutation_feedback = None
        gap_feedback = None
        if previous_summary is not None:
            mutation_feedback = self.orchestrator.run_step(
                StepSpec(
                    name="mutation_feedback",
                    connector="summary_to_mutation_feedback",
                    handler=lambda step_context: build_mutation_feedback(
                        previous_summary,
                        step_context.values["summary"],
                        applied_directives,
                        previous_feedback=previous_mutation_feedback,
                    ),
                    input_roles=self._previous_artifact_roles(
                        "previous_summary",
                        "previous_directives",
                        "previous_mutation_feedback",
                    ),
                    output_roles=self._optional_output_role("mutation_feedback"),
                    metrics=mutation_feedback_metrics,
                ),
                pipeline_context,
            )
            gap_feedback = self.orchestrator.run_step(
                StepSpec(
                    name="gap_feedback",
                    connector="layer3_feedback_to_layer2_feedback",
                    handler=lambda step_context: build_gap_feedback(
                        previous_summary,
                        step_context.values["summary"],
                        applied_directives,
                        previous_gap_feedback=previous_gap_feedback,
                        mutation_feedback=step_context.values["mutation_feedback"],
                    ),
                    input_roles=self._previous_artifact_roles(
                        "previous_summary",
                        "previous_directives",
                        "previous_gap_feedback",
                    ),
                    output_roles=self._optional_output_role("gap_feedback"),
                    metrics=gap_feedback_metrics,
                ),
                pipeline_context,
            )

        layer1_plan = self.orchestrator.run_step(
            StepSpec(
                name="layer1_plan",
                connector="layer2_layer3_feedback_to_layer1_plan",
                handler=lambda step_context: plan_mutations_from_rtl_gaps(
                    step_context.values["summary"],
                    gap_feedback=gap_feedback,
                    mutation_feedback=mutation_feedback,
                ),
                metrics=layer1_plan_metrics,
                metadata={
                    "has_gap_feedback": gap_feedback is not None,
                    "has_mutation_feedback": mutation_feedback is not None,
                },
            ),
            pipeline_context,
        )
        heuristic = self.orchestrator.run_step(
            StepSpec(
                name="heuristic_directives",
                connector="layer1_plan_to_directives",
                handler=lambda step_context: propose_directives_from_plan(
                    step_context.values["summary"],
                    step_context.values["layer1_plan"],
                    gap_feedback=gap_feedback,
                    mutation_feedback=mutation_feedback,
                ),
                output_roles=("directives",),
                metrics=directives_metrics,
                metadata={"source": "heuristic"},
            ),
            pipeline_context,
        )
        prompt = self.orchestrator.run_step(
            StepSpec(
                name="prompt",
                connector="summary_to_llm_prompt",
                handler=lambda step_context: build_llm_prompt(
                    step_context.values["summary"],
                    step_context.values["heuristic_directives"],
                    gap_feedback=gap_feedback,
                    mutation_feedback=mutation_feedback,
                ),
                output_roles=("prompt",),
                metrics=lambda value: {"top_level_keys": len(value)},
            ),
            pipeline_context,
        )

        final_directives = heuristic
        if config.llm:
            final_directives = self._run_llm_path(prompt, heuristic, pipeline_context)

        self._write_outputs(
            summary=summary,
            final_directives=final_directives,
            heuristic=heuristic,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
        )
        return CoverageFeedbackResult(
            summary=summary,
            final_directives=final_directives,
            heuristic_directives=heuristic,
            prompt=prompt,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
        )

    def _run_llm_path(
        self,
        prompt: dict[str, Any],
        heuristic: dict[str, Any],
        pipeline_context: PipelineContext,
    ) -> dict[str, Any]:
        config = self.config
        try:
            llm_value = self.orchestrator.run_step(
                StepSpec(
                    name="llm_response",
                    connector="llm_prompt_to_response",
                    handler=lambda _context: maybe_call_llm(prompt, config.model),
                    output_roles=self._optional_output_role("llm_response"),
                    metrics=lambda value: {"available": value is not None},
                    metadata={"model": config.model},
                ),
                pipeline_context,
            )
            if llm_value is None:
                result = dict(heuristic)
                result["source"] = "heuristic; OPENAI_API_KEY not set"
                return result
            if config.llm_response_out:
                config.llm_response_out.parent.mkdir(parents=True, exist_ok=True)
                config.llm_response_out.write_text(
                    json.dumps(llm_value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            return self.orchestrator.run_step(
                StepSpec(
                    name="final_directives",
                    connector="llm_response_to_directives",
                    handler=lambda _context: validate_directives(config.target, llm_value),
                    output_roles=("directives",),
                    metrics=directives_metrics,
                ),
                pipeline_context,
            )
        except Exception as exc:  # noqa: BLE001 - keep fuzz loop moving with heuristic fallback
            result = dict(heuristic)
            result["source"] = f"heuristic; LLM failed: {exc}"
            return result

    def _write_outputs(
        self,
        *,
        summary: dict[str, Any],
        final_directives: dict[str, Any],
        heuristic: dict[str, Any],
        gap_feedback: dict[str, Any] | None,
        mutation_feedback: dict[str, Any] | None,
    ) -> None:
        config = self.config
        config.summary_out.parent.mkdir(parents=True, exist_ok=True)
        write_json(config.summary_out, summary)
        if config.gap_feedback_out and gap_feedback is not None:
            write_json(config.gap_feedback_out, gap_feedback)
        if config.mutation_feedback_out and mutation_feedback is not None:
            write_json(config.mutation_feedback_out, mutation_feedback)
        write_json(config.directives_out, final_directives)
        write_llm_prompt(config.prompt_out, summary, heuristic)

    def _pipeline_context(self) -> PipelineContext:
        config = self.config
        artifacts = {
            "coverage_info": config.coverage_info,
            "corpus": config.corpus,
            "summary": config.summary_out,
            "directives": config.directives_out,
            "prompt": config.prompt_out,
        }
        optional_artifacts = {
            "coverage_dat": config.coverage_dat,
            "functional_coverage": config.functional_coverage,
            "previous_summary": config.previous_summary,
            "previous_directives": config.previous_directives,
            "previous_gap_feedback": config.previous_gap_feedback,
            "previous_mutation_feedback": config.previous_mutation_feedback,
            "gap_feedback": config.gap_feedback_out,
            "mutation_feedback": config.mutation_feedback_out,
            "llm_response": config.llm_response_out,
        }
        artifacts.update(
            {
                role: path
                for role, path in optional_artifacts.items()
                if path is not None
            }
        )
        return PipelineContext(
            run_id=self.context.run_id,
            artifacts=artifacts,
            metadata={"target": config.target},
        )

    def _summary_input_roles(self) -> tuple[str, ...]:
        roles = ["coverage_info", "corpus"]
        if self.config.coverage_dat is not None:
            roles.append("coverage_dat")
        if self.config.functional_coverage is not None:
            roles.append("functional_coverage")
        return tuple(roles)

    def _previous_artifact_roles(self, *roles: str) -> tuple[str, ...]:
        return tuple(role for role in roles if role in self._pipeline_context().artifacts)

    def _optional_output_role(self, role: str) -> tuple[str, ...]:
        return (role,) if role in self._pipeline_context().artifacts else ()


def run_coverage_feedback_pipeline(
    config: CoverageFeedbackConfig,
    context: ObservationContext,
) -> CoverageFeedbackResult:
    return CoverageFeedbackPipeline(config, context).run()


def read_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def summary_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "uncovered_line_count": int(summary.get("uncovered_line_count", 0)),
        "total_cases": int(summary.get("stimulus_summary", {}).get("total_cases", 0)),
        "open_gap_count": len(summary.get("rtl_gap_summary", {}).get("top_gaps", [])),
    }


def mutation_feedback_metrics(feedback: dict[str, Any]) -> dict[str, Any]:
    return {
        "direction_count": len(feedback.get("directions", {})),
        "attribution": feedback.get("attribution", ""),
    }


def gap_feedback_metrics(feedback: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_gaps": int(feedback.get("total_gaps", 0)),
        "status_counts": feedback.get("status_counts", {}),
        "next_action_counts": feedback.get("next_action_counts", {}),
    }


def layer1_plan_metrics(plan: dict[str, Any]) -> dict[str, Any]:
    selection = plan.get("gap_selection", {})
    selection_counts = {
        key: len(value)
        for key, value in selection.items()
        if isinstance(value, list)
    }
    return {
        "directive_count": len(plan.get("directives", [])),
        "complex_gap_count": len(plan.get("complex_gaps", [])),
        "blocked_count": len(plan.get("blocked", [])),
        "selection_counts": dict(sorted(selection_counts.items())),
        "consumed_gap_feedback": bool(plan.get("feedback_inputs", {}).get("gap_feedback")),
        "consumed_mutation_feedback": bool(
            plan.get("feedback_inputs", {}).get("mutation_feedback")
        ),
    }


def directives_metrics(directives: dict[str, Any]) -> dict[str, Any]:
    items = directives.get("directives", [])
    return {
        "directive_count": len(items) if isinstance(items, list) else 0,
        "source": directives.get("source", ""),
    }
