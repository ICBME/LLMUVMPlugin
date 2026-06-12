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
)
from fuzz_feedback.coverage import build_summary
from fuzz_feedback.feedback_loop import build_gap_feedback, build_mutation_feedback
from fuzz_feedback.mutation_planner import plan_mutations_from_rtl_gaps

from .harness_evidence.runtime_actions import CoverageFeedbackTuningRuntime
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
    heuristic_directives_out: Path | None = None
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
    mode: str | None = None
    coverage_feedback_tuning: Path | None = None
    runtime_metrics_out: Path | None = None


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
        self.coverage_feedback_tuning = CoverageFeedbackTuningRuntime.from_path(
            config.coverage_feedback_tuning,
            metrics_out=config.runtime_metrics_out,
        )
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
                handler=lambda _context: self._build_summary_artifact(),
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
                    handler=lambda step_context: self._build_mutation_feedback_artifact(
                        previous_summary,
                        step_context.values["summary"],
                        applied_directives,
                        previous_mutation_feedback=previous_mutation_feedback,
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
                    handler=lambda step_context: self._build_gap_feedback_artifact(
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
                connector="layer1_plan_to_heuristic_directives",
                handler=lambda step_context: self._build_heuristic_directives_artifact(
                    step_context.values["summary"],
                    step_context.values["layer1_plan"],
                    gap_feedback=gap_feedback,
                    mutation_feedback=mutation_feedback,
                ),
                output_roles=("heuristic_directives",),
                metrics=directives_metrics,
                metadata={"source": "heuristic"},
            ),
            pipeline_context,
        )
        prompt = self.orchestrator.run_step(
            StepSpec(
                name="prompt",
                connector="summary_to_llm_prompt",
                handler=lambda step_context: self._build_prompt_artifact(
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

        if config.llm:
            final_directives = self._run_llm_path(prompt, heuristic, pipeline_context)
        else:
            final_directives = self._materialize_final_directives(
                heuristic,
                pipeline_context,
                source="heuristic",
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
                    handler=lambda _context: self._call_llm_artifact(prompt),
                    output_roles=self._optional_output_role("llm_response"),
                    metrics=lambda value: {"available": value is not None},
                    metadata={"model": config.model},
                ),
                pipeline_context,
            )
            if llm_value is None:
                result = dict(heuristic)
                result["source"] = "heuristic; OPENAI_API_KEY not set"
                return self._materialize_final_directives(
                    result,
                    pipeline_context,
                    source="llm_unavailable_fallback",
                )
            return self.orchestrator.run_step(
                StepSpec(
                    name="final_directives",
                    connector="llm_response_to_directives",
                    handler=lambda _context: self._validate_llm_directives_artifact(
                        llm_value
                    ),
                    output_roles=("directives",),
                    metrics=directives_metrics,
                ),
                pipeline_context,
            )
        except Exception as exc:  # noqa: BLE001 - keep fuzz loop moving with heuristic fallback
            result = dict(heuristic)
            result["source"] = f"heuristic; LLM failed: {exc}"
            return self._materialize_final_directives(
                result,
                pipeline_context,
                source="llm_failed_fallback",
            )

    def _build_summary_artifact(self) -> dict[str, Any]:
        config = self.config
        summary = build_summary(
            config.target,
            config.coverage_info,
            config.corpus,
            coverage_dat=config.coverage_dat,
            functional_coverage=config.functional_coverage,
            ignore_functional_coverage=config.ignore_functional_coverage,
        )
        summary = self.coverage_feedback_tuning.apply_summary(summary)
        write_json(config.summary_out, summary)
        return summary

    def _build_mutation_feedback_artifact(
        self,
        previous_summary: dict[str, Any],
        summary: dict[str, Any],
        applied_directives: dict[str, Any],
        *,
        previous_mutation_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        mutation_feedback = build_mutation_feedback(
            previous_summary,
            summary,
            applied_directives,
            previous_feedback=previous_mutation_feedback,
        )
        if self.config.mutation_feedback_out is not None:
            write_json(self.config.mutation_feedback_out, mutation_feedback)
        return mutation_feedback

    def _build_gap_feedback_artifact(
        self,
        previous_summary: dict[str, Any],
        summary: dict[str, Any],
        applied_directives: dict[str, Any],
        *,
        previous_gap_feedback: dict[str, Any] | None,
        mutation_feedback: dict[str, Any],
    ) -> dict[str, Any]:
        gap_feedback = build_gap_feedback(
            previous_summary,
            summary,
            applied_directives,
            previous_gap_feedback=previous_gap_feedback,
            mutation_feedback=mutation_feedback,
        )
        if self.config.gap_feedback_out is not None:
            write_json(self.config.gap_feedback_out, gap_feedback)
        return gap_feedback

    def _build_heuristic_directives_artifact(
        self,
        summary: dict[str, Any],
        layer1_plan: dict[str, Any],
        *,
        gap_feedback: dict[str, Any] | None,
        mutation_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        heuristic = propose_directives_from_plan(
            summary,
            layer1_plan,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
        )
        heuristic = self.coverage_feedback_tuning.apply_directives(heuristic)
        write_json(self._heuristic_directives_out(), heuristic)
        return heuristic

    def _build_prompt_artifact(
        self,
        summary: dict[str, Any],
        heuristic: dict[str, Any],
        *,
        gap_feedback: dict[str, Any] | None,
        mutation_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prompt = build_llm_prompt(
            summary,
            heuristic,
            gap_feedback=gap_feedback,
            mutation_feedback=mutation_feedback,
        )
        write_json(self.config.prompt_out, prompt)
        return prompt

    def _call_llm_artifact(self, prompt: dict[str, Any]) -> dict[str, Any] | None:
        llm_value = maybe_call_llm(prompt, self.config.model)
        if llm_value is not None and self.config.llm_response_out is not None:
            write_json(self.config.llm_response_out, llm_value)
        return llm_value

    def _validate_llm_directives_artifact(
        self,
        llm_value: dict[str, Any],
    ) -> dict[str, Any]:
        final_directives = validate_directives(self.config.target, llm_value)
        final_directives = self.coverage_feedback_tuning.apply_directives(
            final_directives
        )
        write_json(self.config.directives_out, final_directives)
        return final_directives

    def _materialize_final_directives(
        self,
        directives: dict[str, Any],
        pipeline_context: PipelineContext,
        *,
        source: str,
    ) -> dict[str, Any]:
        return self.orchestrator.run_step(
            StepSpec(
                name="final_directives",
                connector="layer1_plan_to_directives",
                handler=lambda _context: self._write_directives_artifact(directives),
                output_roles=("directives",),
                metrics=directives_metrics,
                metadata={"source": source},
            ),
            pipeline_context,
        )

    def _write_directives_artifact(self, directives: dict[str, Any]) -> dict[str, Any]:
        write_json(self.config.directives_out, directives)
        return directives

    def _pipeline_context(self) -> PipelineContext:
        config = self.config
        artifacts = {
            "coverage_info": config.coverage_info,
            "corpus": config.corpus,
            "summary": config.summary_out,
            "heuristic_directives": self._heuristic_directives_out(),
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
            "coverage_feedback_tuning": config.coverage_feedback_tuning,
            "harness_runtime_metrics": config.runtime_metrics_out,
        }
        artifacts.update(
            {
                role: path
                for role, path in optional_artifacts.items()
                if path is not None
            }
        )
        metadata = {"target": config.target}
        if config.mode is not None:
            metadata["mode"] = config.mode
        return PipelineContext(
            run_id=self.context.run_id,
            artifacts=artifacts,
            metadata=metadata,
        )

    def _summary_input_roles(self) -> tuple[str, ...]:
        roles = ["coverage_info", "corpus"]
        if self.config.coverage_dat is not None:
            roles.append("coverage_dat")
        if self.config.functional_coverage is not None:
            roles.append("functional_coverage")
        return tuple(roles)

    def _heuristic_directives_out(self) -> Path:
        if self.config.heuristic_directives_out is not None:
            return self.config.heuristic_directives_out
        directives = self.config.directives_out
        return directives.with_name(f"{directives.stem}_heuristic{directives.suffix}")

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
