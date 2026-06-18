"""Generic feedback loop for LLM-generated backend artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from ConnectGraph.observation import ObservationRuntime
from ConnectGraph.orchestrator import PipelineContext, PipelineOrchestrator, StepSpec
from LLMPlugin import LLMBackend, LLMBackendError, LLMRequest, LLMResponse
from Spec2Backend.Spec2IR.schema import extract_json

from .schema import (
    CodegenAttempt,
    CodegenEvaluation,
    CodegenEvaluationIssue,
    CodegenLoopConfig,
    CodegenResult,
    FeedbackCodegenError,
    GeneratedFileBundle,
    write_json,
)
from .topology import FEEDBACK_CODEGEN_TOPOLOGY


class FeedbackCodegenTask(Protocol):
    name: str
    artifact_kind: str
    target: str

    def build_prompt(self, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def normalize_response(self, response_json: dict[str, Any]) -> GeneratedFileBundle:
        ...

    def write_candidate(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
    ) -> tuple[Path, ...]:
        ...

    def evaluate(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
    ) -> CodegenEvaluation:
        ...

    def feedback_from_evaluation(
        self,
        evaluation: CodegenEvaluation,
        *,
        attempt_index: int,
        bundle: GeneratedFileBundle | None = None,
    ) -> dict[str, Any]:
        ...

    def promote(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
        final_dir: Path,
    ) -> tuple[Path, ...]:
        ...


def generate_with_feedback(
    task: FeedbackCodegenTask,
    config: CodegenLoopConfig,
    llm_backend: LLMBackend,
    model: str | None = None,
) -> CodegenResult:
    """Run a generic generate/evaluate/feedback loop for one codegen task."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    observation_context = config.observation_context
    topology_out = config.topology_out
    owned_runtime: ObservationRuntime | None = None
    if observation_context is None:
        owned_runtime = ObservationRuntime.from_env(
            topology_out=topology_out,
            stage_id=config.run_name,
        )
        observation_context = owned_runtime.context
        topology_out = topology_out or owned_runtime.topology_out

    orchestrator = PipelineOrchestrator(
        FEEDBACK_CODEGEN_TOPOLOGY,
        observation_context,
        topology_out=topology_out,
    )
    orchestrator.write_topology()

    attempts: list[CodegenAttempt] = []
    feedback: dict[str, Any] | None = None
    final_dir = config.output_dir / "final"

    try:
        for attempt_index in range(1, config.max_attempts + 1):
            attempt_dir = config.output_dir / f"attempt_{attempt_index:03d}"
            paths = _attempt_paths(attempt_dir)
            context = PipelineContext(
                run_id=getattr(observation_context, "run_id", None),
                artifacts={
                    "plan": config.output_dir / "ref_model_plan.json",
                    "prompt": paths["prompt"],
                    "response": paths["response"],
                    "candidate_bundle": paths["candidate_bundle"],
                    "candidate_artifacts": paths["candidate_dir"],
                    "evaluation": paths["evaluation"],
                    "feedback": paths["feedback"],
                    "final_artifact": final_dir,
                },
                metadata={
                    **dict(config.metadata),
                    "task": task.name,
                    "artifact_kind": task.artifact_kind,
                    "target": task.target,
                    "attempt_index": attempt_index,
                },
            )
            bundle: GeneratedFileBundle | None = None

            prompt = orchestrator.run_step(
                StepSpec(
                    name="build_prompt",
                    connector="plan_to_prompt",
                    handler=lambda _ctx, _feedback=feedback: _write_prompt(
                        task,
                        _feedback,
                        paths["prompt"],
                    ),
                    input_roles=("plan",),
                    output_roles=("prompt",),
                    metrics=lambda value: {"feedback_issue_count": _feedback_issue_count(value)},
                    result_key="prompt",
                ),
                context,
            )

            response = orchestrator.run_step(
                StepSpec(
                    name="invoke_llm",
                    connector="prompt_to_llm_response",
                    handler=lambda _ctx, _prompt=prompt: _invoke_llm(
                        llm_backend,
                        _prompt,
                        task=task,
                        config=config,
                        model=model,
                        response_path=paths["response"],
                    ),
                    input_roles=("prompt",),
                    output_roles=("response",),
                    metrics=lambda value: {"has_response": value is not None},
                    result_key="llm_response",
                ),
                context,
            )
            if response is None:
                evaluation = _write_evaluation(
                    paths["evaluation"],
                    CodegenEvaluation.failed(
                        CodegenEvaluationIssue(
                            stage="llm",
                            message="LLM backend returned no response",
                        )
                    ),
                )
                feedback = _run_feedback_step(
                    orchestrator,
                    context,
                    task,
                    evaluation,
                    attempt_index=attempt_index,
                    bundle=None,
                    feedback_path=paths["feedback"],
                )
                attempts.append(_attempt("llm_unavailable", attempt_index, paths, evaluation))
                return CodegenResult(
                    status="llm_unavailable",
                    attempts=tuple(attempts),
                    metadata={"task": task.name, "target": task.target},
                )

            try:
                bundle = orchestrator.run_step(
                    StepSpec(
                        name="normalize_candidate",
                        connector="response_to_candidate",
                        handler=lambda _ctx, _response=response: _normalize_and_write_candidate(
                            task,
                            _response,
                            paths["candidate_bundle"],
                            paths["candidate_dir"],
                        ),
                        input_roles=("response",),
                        output_roles=("candidate_bundle", "candidate_artifacts"),
                        metrics=lambda value: {"file_count": len(value.files)},
                        result_key="candidate_bundle",
                    ),
                    context,
                )
            except _ResponseJsonError as exc:
                evaluation = _write_evaluation(
                    paths["evaluation"],
                    _exception_evaluation("llm_response", exc),
                )
                feedback = _run_feedback_step(
                    orchestrator,
                    context,
                    task,
                    evaluation,
                    attempt_index=attempt_index,
                    bundle=None,
                    feedback_path=paths["feedback"],
                )
                attempts.append(_attempt("llm_invalid_response", attempt_index, paths, evaluation))
                if attempt_index == config.max_attempts:
                    return CodegenResult(
                        status="llm_invalid_response",
                        attempts=tuple(attempts),
                        metadata={"task": task.name, "target": task.target},
                    )
                continue
            except FeedbackCodegenError as exc:
                evaluation = _write_evaluation(
                    paths["evaluation"],
                    _exception_evaluation("response_normalization", exc),
                )
                feedback = _run_feedback_step(
                    orchestrator,
                    context,
                    task,
                    evaluation,
                    attempt_index=attempt_index,
                    bundle=None,
                    feedback_path=paths["feedback"],
                )
                attempts.append(_attempt("invalid_bundle", attempt_index, paths, evaluation))
                if attempt_index == config.max_attempts:
                    return CodegenResult(
                        status="invalid_bundle",
                        attempts=tuple(attempts),
                        metadata={"task": task.name, "target": task.target},
                    )
                continue

            evaluation = orchestrator.run_step(
                StepSpec(
                    name="evaluate_candidate",
                    connector="candidate_to_evaluation",
                    handler=lambda _ctx, _bundle=bundle: _write_evaluation(
                        paths["evaluation"],
                        task.evaluate(_bundle, paths["candidate_dir"]),
                    ),
                    input_roles=("candidate_bundle", "candidate_artifacts"),
                    output_roles=("evaluation",),
                    metrics=lambda value: {
                        "passed": value.passed,
                        "issue_count": len(value.issues),
                        "blocking_issue_count": len(value.blocking_issues),
                    },
                    result_key="evaluation",
                ),
                context,
            )

            feedback = _run_feedback_step(
                orchestrator,
                context,
                task,
                evaluation,
                attempt_index=attempt_index,
                bundle=bundle,
                feedback_path=paths["feedback"],
            )

            if evaluation.passed:
                final_paths = orchestrator.run_step(
                    StepSpec(
                        name="promote_candidate",
                        connector="candidate_to_final_artifact",
                        handler=lambda _ctx, _bundle=bundle: task.promote(
                            _bundle,
                            paths["candidate_dir"],
                            final_dir,
                        ),
                        input_roles=("candidate_bundle", "candidate_artifacts", "evaluation"),
                        output_roles=("final_artifact",),
                        metrics=lambda value: {"file_count": len(value)},
                        result_key="final_paths",
                    ),
                    context,
                )
                attempts.append(_attempt("succeeded", attempt_index, paths, evaluation))
                return CodegenResult(
                    status="succeeded",
                    attempts=tuple(attempts),
                    final_dir=final_dir,
                    final_paths=tuple(final_paths),
                    metadata={"task": task.name, "target": task.target},
                )

            attempts.append(_attempt("evaluation_failed", attempt_index, paths, evaluation))

        return CodegenResult(
            status="max_attempts_exhausted",
            attempts=tuple(attempts),
            metadata={"task": task.name, "target": task.target},
        )
    except LLMBackendError as exc:
        evaluation = CodegenEvaluation.failed(
            CodegenEvaluationIssue(stage="llm", message=str(exc))
        )
        if "paths" in locals() and "attempt_index" in locals():
            _write_evaluation(paths["evaluation"], evaluation)
            attempts.append(_attempt("llm_unavailable", attempt_index, paths, evaluation))
        return CodegenResult(
            status="llm_unavailable",
            attempts=tuple(attempts),
            metadata={"task": task.name, "target": task.target, "error": str(exc)},
        )
    finally:
        if owned_runtime is not None:
            owned_runtime.close()


def llm_response_to_json(response: LLMResponse) -> dict[str, Any]:
    if response.parsed_json is not None:
        return response.parsed_json
    try:
        value = json.loads(extract_json(response.content))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _ResponseJsonError(str(exc)) from exc
    if not isinstance(value, dict):
        raise _ResponseJsonError(f"LLM response JSON must be an object, got {type(value).__name__}")
    return value


def _attempt_paths(attempt_dir: Path) -> dict[str, Path]:
    return {
        "attempt_dir": attempt_dir,
        "prompt": attempt_dir / "prompt.json",
        "response": attempt_dir / "llm_response.json",
        "candidate_bundle": attempt_dir / "candidate_bundle.json",
        "candidate_dir": attempt_dir / "artifacts",
        "evaluation": attempt_dir / "evaluation.json",
        "feedback": attempt_dir / "feedback.json",
    }


def _write_prompt(
    task: FeedbackCodegenTask,
    feedback: dict[str, Any] | None,
    prompt_path: Path,
) -> dict[str, Any]:
    prompt = task.build_prompt(feedback)
    write_json(prompt_path, prompt)
    return prompt


def _invoke_llm(
    backend: LLMBackend,
    prompt: dict[str, Any],
    *,
    task: FeedbackCodegenTask,
    config: CodegenLoopConfig,
    model: str | None,
    response_path: Path,
) -> LLMResponse | None:
    response = backend.invoke(
        LLMRequest(
            prompt=prompt,
            model=model,
            system_prompt=(
                "You generate hardware verification backend artifacts. "
                "Return strict JSON satisfying the response_contract."
            ),
            run_name=config.run_name,
            tags=("feedback-codegen", task.artifact_kind, task.target, *config.tags),
            metadata={"target": task.target, "task": task.name, **dict(config.metadata)},
            response_format="json_object",
        )
    )
    write_json(
        response_path,
        {
            "content": response.content if response is not None else None,
            "parsed_json": response.parsed_json if response is not None else None,
            "metadata": response.metadata if response is not None else {},
        },
    )
    return response


def _normalize_and_write_candidate(
    task: FeedbackCodegenTask,
    response: LLMResponse,
    bundle_path: Path,
    candidate_dir: Path,
) -> GeneratedFileBundle:
    response_json = llm_response_to_json(response)
    bundle = task.normalize_response(response_json)
    write_json(bundle_path, bundle)
    task.write_candidate(bundle, candidate_dir)
    return bundle


def _write_evaluation(path: Path, evaluation: CodegenEvaluation) -> CodegenEvaluation:
    write_json(path, evaluation)
    return evaluation


def _run_feedback_step(
    orchestrator: PipelineOrchestrator,
    context: PipelineContext,
    task: FeedbackCodegenTask,
    evaluation: CodegenEvaluation,
    *,
    attempt_index: int,
    bundle: GeneratedFileBundle | None,
    feedback_path: Path,
) -> dict[str, Any]:
    context.values["evaluation"] = evaluation
    return orchestrator.run_step(
        StepSpec(
            name="build_feedback",
            connector="evaluation_to_feedback",
            handler=lambda _ctx: _write_feedback(
                task,
                evaluation,
                attempt_index=attempt_index,
                bundle=bundle,
                feedback_path=feedback_path,
            ),
            input_roles=("evaluation",),
            output_roles=("feedback",),
            metrics=lambda value: {
                "blocking_issue_count": len(value.get("blocking_issues", [])),
            },
            result_key="feedback",
        ),
        context,
    )


def _write_feedback(
    task: FeedbackCodegenTask,
    evaluation: CodegenEvaluation,
    *,
    attempt_index: int,
    bundle: GeneratedFileBundle | None,
    feedback_path: Path,
) -> dict[str, Any]:
    feedback = task.feedback_from_evaluation(
        evaluation,
        attempt_index=attempt_index,
        bundle=bundle,
    )
    write_json(feedback_path, feedback)
    return feedback


def _exception_evaluation(stage: str, exc: Exception) -> CodegenEvaluation:
    return CodegenEvaluation.failed(
        CodegenEvaluationIssue(
            stage=stage,
            message=f"{type(exc).__name__}: {exc}",
        )
    )


def _attempt(
    status: str,
    index: int,
    paths: dict[str, Path],
    evaluation: CodegenEvaluation,
) -> CodegenAttempt:
    return CodegenAttempt(
        index=index,
        status=status,
        attempt_dir=paths["attempt_dir"],
        prompt_path=paths["prompt"],
        response_path=paths["response"],
        candidate_bundle_path=paths["candidate_bundle"],
        candidate_dir=paths["candidate_dir"],
        evaluation_path=paths["evaluation"],
        feedback_path=paths["feedback"],
        evaluation=evaluation,
    )


def _feedback_issue_count(prompt: dict[str, Any]) -> int:
    feedback = prompt.get("feedback")
    if not isinstance(feedback, dict):
        return 0
    issues = feedback.get("blocking_issues", [])
    return len(issues) if isinstance(issues, list) else 0


class _ResponseJsonError(FeedbackCodegenError):
    pass


__all__ = [
    "FeedbackCodegenTask",
    "generate_with_feedback",
    "llm_response_to_json",
]
