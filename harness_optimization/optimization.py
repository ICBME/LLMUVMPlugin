from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import request

from .io import path_or_none, read_optional_json, write_json
from .records import list_value, mapping, optional_path


@dataclass(frozen=True)
class HarnessOptimizationPaths:
    task: Path
    proposal: Path
    decision: Path
    advice_report: Path
    optimizer_prompt: Path
    optimizer_response: Path
    patch: Path
    candidate_manifest: Path
    candidate_evaluation: Path
    metric_delta: Path
    final_decision: Path
    sandbox_dir: Path

    def to_json(self) -> dict[str, str]:
        return {
            "harness_optimization_task": str(self.task),
            "harness_optimization_proposal": str(self.proposal),
            "harness_optimization_decision": str(self.decision),
        }

    def advice_json(self) -> dict[str, str]:
        return {
            "harness_optimization_advice_report": str(self.advice_report),
        }

    def optimizer_io_json(self) -> dict[str, str]:
        return {
            "harness_optimization_optimizer_prompt": str(self.optimizer_prompt),
            "harness_optimization_optimizer_response": str(self.optimizer_response),
        }

    def validation_json(self) -> dict[str, str]:
        return {
            "harness_optimization_patch": str(self.patch),
            "harness_optimization_candidate_manifest": str(self.candidate_manifest),
            "harness_optimization_candidate_evaluation": str(
                self.candidate_evaluation
            ),
            "harness_optimization_metric_delta": str(self.metric_delta),
            "harness_optimization_final_decision": str(self.final_decision),
            "harness_optimization_sandbox": str(self.sandbox_dir),
        }


@dataclass(frozen=True)
class HarnessOptimizerContext:
    paths: HarnessOptimizationPaths
    cwd: Path


@dataclass(frozen=True)
class OptimizationTaskArtifacts:
    harness_trace: dict[str, Any]
    harness_evaluation_path: Path | None
    llm_dataset_path: Path | None
    campaign_rollup_path: Path | None
    action_effect_report_path: Path | None
    harness_evaluation: dict[str, Any]
    campaign_rollup: dict[str, Any]
    action_effect_report: dict[str, Any]


class HarnessOptimizerBackend(Protocol):
    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        ...


class HarnessCandidateEvaluationBackend(Protocol):
    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class HarnessLlmTransport(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        ...


ProposalValidator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
PromptBuilder = Callable[..., dict[str, Any]]
BaselineMetricSnapshot = Callable[[dict[str, Any]], dict[str, Any]]
CandidateEvaluationNormalizer = Callable[
    [Any, dict[str, Any], dict[str, Any], str],
    dict[str, Any],
]
CandidateEvaluationErrorBuilder = Callable[
    [dict[str, Any], dict[str, Any], str, str, str],
    dict[str, Any],
]


@dataclass(frozen=True)
class OpenAICompatibleChatTransport:
    endpoint: str
    api_key: str | None = None
    timeout: float = 60.0
    extra_headers: Mapping[str, str] | None = None

    @classmethod
    def from_env(cls) -> "OpenAICompatibleChatTransport":
        endpoint = os.getenv(
            "HARNESS_OPTIMIZER_LLM_ENDPOINT",
            "https://api.openai.com/v1/chat/completions",
        )
        api_key = os.getenv("HARNESS_OPTIMIZER_LLM_API_KEY") or os.getenv(
            "OPENAI_API_KEY"
        )
        return cls(endpoint=endpoint, api_key=api_key)

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.extra_headers:
            headers.update(dict(self.extra_headers))
        req = request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
        return json.loads(body)


class NoopOptimizationProposalBackend:
    def __init__(
        self,
        *,
        proposal_kind: str,
        rationale: str,
        source: str = "noop",
    ) -> None:
        object.__setattr__(self, "proposal_kind", proposal_kind)
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "source", source)

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.proposal_kind,
            "proposal_id": f"{task.get('run_id') or 'unknown'}:noop",
            "created_at": utc_timestamp(),
            "source": self.source,
            "status": "no_op",
            "actions": [],
            "evidence_refs": [],
            "rationale": self.rationale,
        }


class LlmOptimizationProposalBackend:
    def __init__(
        self,
        *,
        model: str,
        prompt_builder: PromptBuilder,
        validator: ProposalValidator,
        proposal_kind: str,
        response_kind: str,
        transport: HarnessLlmTransport | None = None,
        source: str = "llm",
        temperature: float = 0.0,
        max_repair_attempts: int = 1,
        sample_limit: int = 5,
    ) -> None:
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "prompt_builder", prompt_builder)
        object.__setattr__(self, "validator", validator)
        object.__setattr__(self, "proposal_kind", proposal_kind)
        object.__setattr__(self, "response_kind", response_kind)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "max_repair_attempts", max_repair_attempts)
        object.__setattr__(self, "sample_limit", sample_limit)

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "LlmOptimizationProposalBackend requires run_with_context() so prompt "
            "and response artifacts can be materialized."
        )

    def run_with_context(
        self,
        task: dict[str, Any],
        context: HarnessOptimizerContext,
    ) -> dict[str, Any]:
        transport = self.transport or OpenAICompatibleChatTransport.from_env()
        prompt = _call_prompt_builder(
            self.prompt_builder,
            task,
            sample_limit=self.sample_limit,
        )
        write_json(context.paths.optimizer_prompt, prompt)
        attempts: list[dict[str, Any]] = []
        messages = list(prompt["messages"])
        final_proposal: dict[str, Any] | None = None
        final_validation: dict[str, Any] | None = None
        for attempt_index in range(self.max_repair_attempts + 1):
            raw_response = transport.complete(
                messages=messages,
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
            proposal = proposal_from_llm_response(
                raw_response,
                task=task,
                source=self.source,
                proposal_kind=self.proposal_kind,
                attempt_index=attempt_index,
            )
            validation = self.validator(proposal, task)
            attempts.append(
                {
                    "attempt_index": attempt_index,
                    "valid": validation["valid"],
                    "validation": validation,
                    "response": compact_llm_response(raw_response),
                    "proposal": proposal,
                }
            )
            final_proposal = proposal
            final_validation = validation
            if validation["valid"]:
                break
            if attempt_index < self.max_repair_attempts:
                messages = build_repair_messages(prompt, proposal, validation)
        response_artifact = {
            "schema_version": 1,
            "kind": self.response_kind,
            "created_at": utc_timestamp(),
            "model": self.model,
            "source": self.source,
            "attempt_count": len(attempts),
            "repaired": any(not item["valid"] for item in attempts[:-1]),
            "attempts": attempts,
        }
        write_json(context.paths.optimizer_response, response_artifact)
        proposal = final_proposal or invalid_optimizer_proposal(
            task,
            source=self.source,
            proposal_kind=self.proposal_kind,
            reason="optimizer produced no response",
        )
        if final_validation is not None and not final_validation["valid"]:
            proposal["status"] = "invalid"
            proposal["schema_errors"] = final_validation["errors"]
        proposal.setdefault("llm_provenance", {})
        proposal["llm_provenance"].update(
            {
                "source": self.source,
                "model": self.model,
                "prompt_artifact": str(context.paths.optimizer_prompt),
                "response_artifact": str(context.paths.optimizer_response),
                "attempt_count": len(attempts),
                "repaired": any(not item["valid"] for item in attempts[:-1]),
                "final_valid": bool(final_validation and final_validation["valid"]),
            }
        )
        proposal.setdefault("artifacts", {})
        proposal["artifacts"].update(
            {
                "optimizer_prompt": str(context.paths.optimizer_prompt),
                "optimizer_response": str(context.paths.optimizer_response),
            }
        )
        return proposal


class PromptOnlyOptimizationProposalBackend:
    def __init__(
        self,
        *,
        model: str,
        prompt_builder: PromptBuilder,
        proposal_kind: str,
        response_kind: str,
        source: str = "prompt_only",
        sample_limit: int = 5,
        proposal_rationale: str | None = None,
        response_reason: str | None = None,
    ) -> None:
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "prompt_builder", prompt_builder)
        object.__setattr__(self, "proposal_kind", proposal_kind)
        object.__setattr__(self, "response_kind", response_kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "sample_limit", sample_limit)
        object.__setattr__(
            self,
            "proposal_rationale",
            proposal_rationale or (
            "Prompt-only backend generated no optimization actions; use the prompt "
            "artifact with an external LLM or switch to the llm backend for "
            "structured suggestions."
            ),
        )
        object.__setattr__(
            self,
            "response_reason",
            response_reason or (
            "Prompt-only optimizer backend wrote the optimizer prompt for external "
            "review and intentionally skipped the LLM transport call."
            ),
        )

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "PromptOnlyOptimizationProposalBackend requires run_with_context() so "
            "the prompt artifact can be materialized."
        )

    def run_with_context(
        self,
        task: dict[str, Any],
        context: HarnessOptimizerContext,
    ) -> dict[str, Any]:
        prompt = _call_prompt_builder(
            self.prompt_builder,
            task,
            sample_limit=self.sample_limit,
        )
        write_json(context.paths.optimizer_prompt, prompt)
        response_artifact = {
            "schema_version": 1,
            "kind": self.response_kind,
            "created_at": utc_timestamp(),
            "model": self.model,
            "source": self.source,
            "status": "not_called",
            "attempt_count": 0,
            "repaired": False,
            "attempts": [],
            "reason": self.response_reason,
        }
        write_json(context.paths.optimizer_response, response_artifact)
        return {
            "schema_version": 1,
            "kind": self.proposal_kind,
            "proposal_id": f"{task.get('run_id') or 'unknown'}:prompt_only",
            "created_at": utc_timestamp(),
            "source": self.source,
            "status": "no_op",
            "actions": [],
            "evidence_refs": [],
            "rationale": self.proposal_rationale,
            "llm_provenance": {
                "source": self.source,
                "model": self.model,
                "prompt_artifact": str(context.paths.optimizer_prompt),
                "response_artifact": str(context.paths.optimizer_response),
                "attempt_count": 0,
                "transport_called": False,
                "final_valid": True,
            },
            "artifacts": {
                "optimizer_prompt": str(context.paths.optimizer_prompt),
                "optimizer_response": str(context.paths.optimizer_response),
            },
        }


class NoopOptimizationCandidateEvaluationBackend:
    def __init__(
        self,
        *,
        candidate_evaluation_kind: str,
        baseline_metric_snapshot: BaselineMetricSnapshot,
        rationale: str,
        source: str = "noop",
    ) -> None:
        object.__setattr__(
            self,
            "candidate_evaluation_kind",
            candidate_evaluation_kind,
        )
        object.__setattr__(
            self,
            "baseline_metric_snapshot",
            baseline_metric_snapshot,
        )
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "source", source)

    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        baseline_metrics = self.baseline_metric_snapshot(task)
        return {
            "schema_version": 1,
            "kind": self.candidate_evaluation_kind,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "source": self.source,
            "status": "not_run",
            "application_status": patch.get("status"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": dict(baseline_metrics),
            "summary": {
                "candidate_artifact_count": len(
                    list_value(candidate_manifest.get("candidate_artifacts"))
                ),
                "sandbox_validation": "not_configured",
            },
            "reason": self.rationale,
        }


def _call_prompt_builder(
    prompt_builder: PromptBuilder,
    task: dict[str, Any],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    try:
        parameters = tuple(inspect.signature(prompt_builder).parameters.values())
    except (TypeError, ValueError):
        return prompt_builder(task, sample_limit)

    supports_keyword = False
    for parameter in parameters[1:]:
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            supports_keyword = True
            break
        if (
            parameter.name == "sample_limit"
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ):
            supports_keyword = True
            break

    if supports_keyword:
        return prompt_builder(task, sample_limit=sample_limit)
    return prompt_builder(task, sample_limit)


@dataclass(frozen=True)
class OptimizationRuntimeAdapter:
    paths: HarnessOptimizationPaths
    cwd: Path
    optimizer_backend: HarnessOptimizerBackend
    candidate_evaluation_backend: HarnessCandidateEvaluationBackend
    proposal_kind: str
    normalize_candidate_evaluation: CandidateEvaluationNormalizer
    candidate_evaluation_error: CandidateEvaluationErrorBuilder

    def run_proposal(self, task: dict[str, Any]) -> dict[str, Any]:
        try:
            runner = getattr(self.optimizer_backend, "run_with_context", None)
            if callable(runner):
                proposal = runner(
                    task,
                    HarnessOptimizerContext(paths=self.paths, cwd=self.cwd),
                )
            else:
                proposal = self.optimizer_backend.run(task)
        except Exception as exc:  # noqa: BLE001 - caller decides whether invalid proposal blocks flow
            proposal = {
                "schema_version": 1,
                "kind": self.proposal_kind,
                "proposal_id": f"{task.get('run_id') or 'unknown'}:backend_error",
                "created_at": utc_timestamp(),
                "source": type(self.optimizer_backend).__name__,
                "status": "invalid",
                "actions": [],
                "evidence_refs": [],
                "rationale": "optimizer backend raised an exception",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        if not isinstance(proposal, dict):
            proposal = {
                "schema_version": 1,
                "kind": self.proposal_kind,
                "proposal_id": f"{task.get('run_id') or 'unknown'}:invalid",
                "created_at": utc_timestamp(),
                "source": type(self.optimizer_backend).__name__,
                "status": "invalid",
                "actions": [],
                "evidence_refs": [],
                "rationale": "optimizer backend returned a non-object proposal",
                "raw_type": type(proposal).__name__,
            }
        write_json(self.paths.proposal, proposal)
        return proposal

    def run_candidate_evaluation(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            evaluation = self.candidate_evaluation_backend.run(
                task,
                proposal,
                patch,
                candidate_manifest,
            )
        except Exception as exc:  # noqa: BLE001 - final decision layer handles failed validation
            evaluation = self.candidate_evaluation_error(
                task,
                proposal,
                type(self.candidate_evaluation_backend).__name__,
                type(exc).__name__,
                str(exc),
            )
        evaluation = self.normalize_candidate_evaluation(
            evaluation,
            task,
            proposal,
            type(self.candidate_evaluation_backend).__name__,
        )
        write_json(self.paths.candidate_evaluation, evaluation)
        return evaluation


def count_jsonl_items(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                count += 1
    return count


def resolve_artifact_path(value: object, cwd: Path) -> Path | None:
    path = path_or_none(value)
    if path is None:
        return None
    if path.is_absolute():
        return path
    return cwd / path


def load_optimization_task_artifacts(
    campaign_evaluation: dict[str, Any],
    campaign_manifest: dict[str, Any],
    *,
    cwd: Path,
    action_effect_report_role: str = "candidate_action_effect_report",
) -> OptimizationTaskArtifacts:
    harness_trace = mapping(campaign_evaluation.get("harness_trace"))
    trace_artifacts = mapping(harness_trace.get("artifacts"))
    harness_evaluation_path = resolve_artifact_path(
        trace_artifacts.get("harness_evaluation"),
        cwd,
    )
    llm_dataset_path = resolve_artifact_path(
        trace_artifacts.get("llm_optimization_dataset"),
        cwd,
    )
    campaign_rollup_path = resolve_artifact_path(
        trace_artifacts.get("campaign_trace_rollup"),
        cwd,
    )
    action_effect_report_path = resolve_artifact_path(
        trace_artifacts.get(action_effect_report_role)
        or mapping(campaign_manifest.get("artifacts")).get(action_effect_report_role),
        cwd,
    )
    return OptimizationTaskArtifacts(
        harness_trace=harness_trace,
        harness_evaluation_path=harness_evaluation_path,
        llm_dataset_path=llm_dataset_path,
        campaign_rollup_path=campaign_rollup_path,
        action_effect_report_path=action_effect_report_path,
        harness_evaluation=read_optional_json(harness_evaluation_path) or {},
        campaign_rollup=read_optional_json(campaign_rollup_path) or {},
        action_effect_report=read_optional_json(action_effect_report_path) or {},
    )


def build_optimization_task(
    *,
    task_kind: str,
    campaign_evaluation: dict[str, Any],
    campaign_manifest: dict[str, Any],
    campaign_evaluation_path: Path,
    campaign_manifest_path: Path,
    target: str,
    cwd: Path,
    objective: str,
    constraints: dict[str, Any],
    evidence_index_builder: Callable[[dict[str, Any]], dict[str, Any]],
    action_effect_report_role: str = "candidate_action_effect_report",
) -> dict[str, Any]:
    artifacts = load_optimization_task_artifacts(
        campaign_evaluation,
        campaign_manifest,
        cwd=cwd,
        action_effect_report_role=action_effect_report_role,
    )
    harness_trace = artifacts.harness_trace
    evidence_index = evidence_index_builder(artifacts.harness_evaluation)
    return {
        "schema_version": 1,
        "kind": task_kind,
        "created_at": utc_timestamp(),
        "target": campaign_evaluation.get("target")
        or campaign_manifest.get("target")
        or target,
        "run_id": campaign_evaluation.get("run_id") or campaign_manifest.get("run_id"),
        "sources": {
            "campaign_evaluation": str(campaign_evaluation_path),
            "campaign_manifest": str(campaign_manifest_path),
        },
        "artifacts": {
            "campaign_evaluation": str(campaign_evaluation_path),
            "campaign_manifest": str(campaign_manifest_path),
            **optional_path("harness_evaluation", artifacts.harness_evaluation_path),
            **optional_path("llm_optimization_dataset", artifacts.llm_dataset_path),
            **optional_path("campaign_trace_rollup", artifacts.campaign_rollup_path),
            **optional_path(
                action_effect_report_role,
                artifacts.action_effect_report_path,
            ),
        },
        "summary": {
            "campaign": mapping(campaign_evaluation.get("summary")),
            "harness": mapping(artifacts.harness_evaluation.get("summary"))
            or mapping(harness_trace.get("summary")),
            "campaign_rollup": mapping(artifacts.campaign_rollup.get("summary"))
            or mapping(harness_trace.get("campaign_rollup", {})).get("summary", {}),
            "llm_sample_count": count_jsonl_items(artifacts.llm_dataset_path),
            "action_effect": mapping(artifacts.action_effect_report.get("summary")),
        },
        "optimization_hints": mapping(
            artifacts.harness_evaluation.get("optimization_hints")
        )
        or mapping(harness_trace.get("optimization_hints")),
        "trace_quality": mapping(artifacts.harness_evaluation.get("trace_quality")),
        "failure_clusters": list_value(artifacts.harness_evaluation.get("failure_clusters"))[
            :5
        ],
        "slowest_records": list_value(artifacts.harness_evaluation.get("slowest_records"))[
            :5
        ],
        "coverage_trends": list_value(artifacts.campaign_rollup.get("coverage_trends"))[
            :10
        ],
        "failure_trends": list_value(artifacts.campaign_rollup.get("failure_trends"))[
            :10
        ],
        "action_effect_report": (
            artifacts.action_effect_report if artifacts.action_effect_report else {}
        ),
        "evidence_index": evidence_index,
        "constraints": constraints,
        "objective": objective,
    }


def build_optimization_prompt(
    *,
    task: dict[str, Any],
    sample_limit: int,
    prompt_kind: str,
    schema_hint: dict[str, Any],
    system_message: str,
) -> dict[str, Any]:
    artifacts = mapping(task.get("artifacts"))
    dataset_path = path_or_none(artifacts.get("llm_optimization_dataset"))
    dataset_samples = read_jsonl_samples(dataset_path, limit=sample_limit)
    context = {
        "task_kind": task.get("kind"),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "summary": mapping(task.get("summary")),
        "optimization_hints": mapping(task.get("optimization_hints")),
        "trace_quality": mapping(task.get("trace_quality")),
        "failure_clusters": list_value(task.get("failure_clusters"))[:5],
        "slowest_records": list_value(task.get("slowest_records"))[:5],
        "coverage_trends": list_value(task.get("coverage_trends"))[:10],
        "failure_trends": list_value(task.get("failure_trends"))[:10],
        "action_effect_report": mapping(task.get("action_effect_report")),
        "llm_dataset_samples": dataset_samples,
        "evidence_index": mapping(task.get("evidence_index")),
    }
    user = json.dumps(
        {
            "objective": task.get("objective"),
            "proposal_schema": schema_hint,
            "context": context,
        },
        indent=2,
        sort_keys=True,
    )
    return {
        "schema_version": 1,
        "kind": prompt_kind,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "schema_hint": schema_hint,
        "context": context,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user},
        ],
    }


def optimization_advice_action_summary(
    action: dict[str, Any],
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    constraints = mapping(task.get("constraints"))
    safe_types = set(
        str(item) for item in list_value(constraints.get("safe_sandbox_action_types"))
    )
    action_type = str(action.get("action_type") or "")
    return {
        "action_id": action.get("action_id"),
        "action_type": action_type,
        "sandbox_safe": action_type in safe_types,
        "payload": mapping(action.get("payload")),
        "rationale": action.get("rationale") or action.get("reason"),
        "evidence_refs": list_value(action.get("evidence_refs"))
        or list_value(proposal.get("evidence_refs"))
        or list_value(action.get("evidence")),
        "requires_candidate_validation": True,
    }


def build_optimization_advice_report(
    *,
    kind: str,
    task: dict[str, Any],
    proposal: dict[str, Any],
    decision: dict[str, Any],
    paths: HarnessOptimizationPaths | None = None,
    action_summary_builder: Callable[
        [dict[str, Any], dict[str, Any], dict[str, Any]],
        dict[str, Any],
    ] = optimization_advice_action_summary,
    suggested_candidate_controls: dict[str, Any] | None = None,
    suggested_campaign_plan_profile: str | None = (
        "campaign_with_evaluation_and_optimization_real_validation"
    ),
    suggested_candidate_backend: str | None = "real",
) -> dict[str, Any]:
    actions = [
        item for item in list_value(proposal.get("actions")) if isinstance(item, dict)
    ]
    accepted = decision.get("decision") == "accepted"
    recommended_actions = [
        action_summary_builder(action, task, proposal)
        for action in actions
        if accepted and proposal.get("status") == "proposed"
    ]
    validation_errors = list_value(mapping(decision.get("validation")).get("errors"))
    if not accepted:
        status = "rejected"
    elif not recommended_actions:
        status = "no_action"
    else:
        status = "ready_for_candidate_validation"
    artifact_refs: dict[str, str] = {}
    if paths is not None:
        artifact_refs.update(
            {
                "harness_optimization_task": str(paths.task),
                "harness_optimization_proposal": str(paths.proposal),
                "harness_optimization_decision": str(paths.decision),
                "harness_optimization_advice_report": str(paths.advice_report),
            }
        )
    proposal_artifacts = mapping(proposal.get("artifacts"))
    for role in ("optimizer_prompt", "optimizer_response"):
        if proposal_artifacts.get(role):
            artifact_refs[role] = str(proposal_artifacts[role])
    if suggested_candidate_controls is None and recommended_actions:
        suggested_candidate_controls = {
            "matched_baseline": True,
            "paired_repeats": 3,
            "attribution_mode": "all_actions",
            "min_improved_metrics": 1,
            "max_regressed_metrics": 0,
            "max_flaky_metrics": 0,
        }
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "status": status,
        "source": proposal.get("source"),
        "summary": {
            "decision": decision.get("decision"),
            "proposal_status": proposal.get("status"),
            "action_count": len(actions),
            "recommended_action_count": len(recommended_actions),
            "validation_error_count": len(validation_errors),
            "llm_sample_count": mapping(task.get("summary")).get(
                "llm_sample_count",
                0,
            ),
            "requires_candidate_validation": bool(recommended_actions),
        },
        "advice_only_guard": {
            "sandbox_apply_triggered": False,
            "candidate_regression_triggered": False,
            "mainline_apply_triggered": False,
            "candidate_backend_required": False,
            "scope": "schema_and_evidence_review_only",
        },
        "validation_scope": {
            "schema_validation": True,
            "evidence_ref_validation": True,
            "safe_action_dsl_validation": True,
            "candidate_metric_validation": False,
            "matched_noop_baseline": False,
            "paired_repeated_validation": False,
        },
        "observations": {
            "campaign_summary": mapping(mapping(task.get("summary")).get("campaign")),
            "harness_summary": mapping(mapping(task.get("summary")).get("harness")),
            "campaign_rollup_summary": mapping(
                mapping(task.get("summary")).get("campaign_rollup")
            ),
            "action_effect_summary": mapping(
                mapping(task.get("summary")).get("action_effect")
            ),
            "optimization_hints": mapping(task.get("optimization_hints")),
            "trace_quality": mapping(task.get("trace_quality")),
            "failure_clusters": list_value(task.get("failure_clusters")),
            "coverage_trends": list_value(task.get("coverage_trends")),
        },
        "recommended_actions": recommended_actions,
        "invalid_or_rejected_reasons": validation_errors,
        "handoff": {
            "requires_candidate_validation": bool(recommended_actions),
            "suggested_campaign_plan_profile": (
                suggested_campaign_plan_profile if recommended_actions else None
            ),
            "suggested_candidate_backend": (
                suggested_candidate_backend if recommended_actions else None
            ),
            "suggested_candidate_controls": (
                suggested_candidate_controls if recommended_actions else {}
            ),
        },
        "reproducibility": {
            "artifacts": artifact_refs,
            "source_artifacts": mapping(task.get("artifacts")),
            "plugin_registry_fingerprint": mapping(
                mapping(task.get("constraints")).get("plugin_registry")
            ).get("fingerprint"),
            "plugin_validation": mapping(
                mapping(task.get("constraints")).get("plugin_validation")
            ),
            "llm_provenance": mapping(proposal.get("llm_provenance")),
        },
    }


def harness_optimization_paths(evaluation_path: Path) -> HarnessOptimizationPaths:
    stem = evaluation_path.stem
    sandbox_dir = evaluation_path.with_name(f"{stem}_harness_optimization_sandbox")
    return HarnessOptimizationPaths(
        task=evaluation_path.with_name(f"{stem}_harness_optimization_task.json"),
        advice_report=evaluation_path.with_name(
            f"{stem}_harness_optimization_advice_report.json"
        ),
        optimizer_prompt=evaluation_path.with_name(
            f"{stem}_harness_optimization_optimizer_prompt.json"
        ),
        optimizer_response=evaluation_path.with_name(
            f"{stem}_harness_optimization_optimizer_response.json"
        ),
        proposal=evaluation_path.with_name(
            f"{stem}_harness_optimization_proposal.json"
        ),
        decision=evaluation_path.with_name(
            f"{stem}_harness_optimization_decision.json"
        ),
        patch=evaluation_path.with_name(f"{stem}_harness_optimization_patch.json"),
        candidate_manifest=evaluation_path.with_name(
            f"{stem}_harness_optimization_candidate_manifest.json"
        ),
        candidate_evaluation=evaluation_path.with_name(
            f"{stem}_harness_optimization_candidate_evaluation.json"
        ),
        metric_delta=evaluation_path.with_name(
            f"{stem}_harness_optimization_metric_delta.json"
        ),
        final_decision=evaluation_path.with_name(
            f"{stem}_harness_optimization_final_decision.json"
        ),
        sandbox_dir=sandbox_dir,
    )


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_jsonl_samples(path: Path | None, *, limit: int) -> list[dict[str, Any]]:
    if path is None or not path.exists() or limit <= 0:
        return []
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if len(samples) >= limit:
            break
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            samples.append(value)
    return samples


def proposal_from_llm_response(
    response: Any,
    *,
    task: dict[str, Any],
    source: str,
    proposal_kind: str,
    attempt_index: int,
) -> dict[str, Any]:
    value = llm_response_json(response)
    if isinstance(value, dict) and isinstance(value.get("proposal"), dict):
        value = value["proposal"]
    if not isinstance(value, dict):
        return invalid_optimizer_proposal(
            task,
            source=source,
            proposal_kind=proposal_kind,
            reason="LLM response did not contain a JSON object proposal",
            attempt_index=attempt_index,
        )
    proposal = dict(value)
    proposal.setdefault("schema_version", 1)
    proposal.setdefault("kind", proposal_kind)
    proposal.setdefault(
        "proposal_id",
        f"{task.get('run_id') or 'unknown'}:llm:{attempt_index}",
    )
    proposal.setdefault("created_at", utc_timestamp())
    proposal.setdefault("source", source)
    proposal.setdefault("status", "proposed" if proposal.get("actions") else "no_op")
    proposal.setdefault("actions", [])
    proposal.setdefault("evidence_refs", [])
    proposal.setdefault("rationale", "LLM-generated harness optimization proposal")
    return proposal


def llm_response_json(response: Any) -> Any:
    if isinstance(response, str):
        return parse_json_text(response)
    if not isinstance(response, dict):
        return response
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            content = mapping(first.get("message")).get("content")
            return parse_json_text(content) if isinstance(content, str) else content
    content = response.get("content")
    if isinstance(content, str):
        return parse_json_text(content)
    return response


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return text
        return text


def invalid_optimizer_proposal(
    task: dict[str, Any],
    *,
    source: str,
    proposal_kind: str,
    reason: str,
    attempt_index: int | None = None,
) -> dict[str, Any]:
    suffix = "invalid" if attempt_index is None else f"invalid:{attempt_index}"
    return {
        "schema_version": 1,
        "kind": proposal_kind,
        "proposal_id": f"{task.get('run_id') or 'unknown'}:{suffix}",
        "created_at": utc_timestamp(),
        "source": source,
        "status": "invalid",
        "actions": [],
        "evidence_refs": [],
        "rationale": reason,
    }


def compact_llm_response(response: Any) -> Any:
    if isinstance(response, dict):
        compact = {
            key: response.get(key)
            for key in ("id", "model", "object", "created", "usage")
            if key in response
        }
        if "choices" in response:
            compact["choices"] = response["choices"]
        return compact or response
    return response


def build_repair_messages(
    prompt: dict[str, Any],
    proposal: dict[str, Any],
    validation: dict[str, Any],
) -> list[dict[str, str]]:
    messages = list(prompt["messages"])
    messages.append(
        {
            "role": "assistant",
            "content": json.dumps(proposal, indent=2, sort_keys=True),
        }
    )
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "repair_request": (
                        "The proposal failed schema validation. Return a repaired "
                        "proposal JSON object only."
                    ),
                    "validation_errors": validation.get("errors", []),
                    "proposal_schema": prompt.get("schema_hint"),
                },
                indent=2,
                sort_keys=True,
            ),
        }
    )
    return messages
