from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ConnectGraph.trace import read_json_object
from harness_optimization.io import write_json
from harness_optimization.optimization import (
    HarnessCandidateEvaluationBackend,
    HarnessLlmTransport,
    HarnessOptimizationPaths,
    HarnessOptimizerBackend,
    HarnessOptimizerContext,
    OpenAICompatibleChatTransport,
    build_repair_messages,
    compact_llm_response,
    harness_optimization_paths,
    invalid_optimizer_proposal,
    proposal_from_llm_response,
    read_jsonl_samples,
    utc_timestamp,
)
from harness_optimization.records import list_value
from harness_optimization.rules import (
    action_payload_errors as _action_payload_errors,
    baseline_metric_snapshot,
    build_candidate_final_decision as _build_candidate_final_decision,
    build_candidate_metric_delta as _build_candidate_metric_delta,
    builtin_safe_action_dsl_schema as _builtin_safe_action_dsl_schema,
    candidate_evaluation_error as _candidate_evaluation_error,
    candidate_id_for,
    evidence_index_for,
    evidence_ref_error,
    final_decision_thresholds,
    int_value,
    metric_direction,
    metric_gates_acceptance,
    metric_role,
    normalize_candidate_evaluation as _normalize_candidate_evaluation,
    number_value,
    numeric_metrics,
    optimizer_proposal_schema_hint as _optimizer_proposal_schema_hint,
    replay_probe_payload_errors,
    safe_action_dsl_schema as _safe_action_dsl_schema,
    safe_slug,
    scoreboard_check_payload_errors,
    summary_metric_count,
    validate_harness_optimization_proposal as _validate_harness_optimization_proposal,
    coverage_feedback_tuning_payload_errors,
    mmio_readback_payload_errors,
)

from .plugins import (
    HarnessActionPlugin,
    HarnessPluginRegistry,
    plugin_registry_provenance_json,
    plugin_registry_to_json,
    plugin_registry_validation_json,
)
from .records import mapping


TASK_KIND = "libafl_bfm_fuzz.harness_optimization_task"
PROPOSAL_KIND = "libafl_bfm_fuzz.harness_optimization_proposal"
DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_decision"
PATCH_KIND = "libafl_bfm_fuzz.harness_optimization_patch"
CANDIDATE_MANIFEST_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_manifest"
CANDIDATE_EVALUATION_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_evaluation"
METRIC_DELTA_KIND = "libafl_bfm_fuzz.harness_optimization_metric_delta"
FINAL_DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_final_decision"
ADVICE_REPORT_KIND = "libafl_bfm_fuzz.harness_optimization_advice_report"

ALLOWED_ACTION_TYPES = (
    "mutation_directive_update",
    "stimulus_generation_hint",
    "replay_probe",
    "scoreboard_check",
    "ref_model_patch",
    "coverage_feedback_tuning",
    "mmio_readback",
    "documentation_note",
    "no_op",
)

SAFE_SANDBOX_ACTION_TYPES = (
    "mutation_directive_update",
    "stimulus_generation_hint",
    "replay_probe",
    "scoreboard_check",
    "coverage_feedback_tuning",
    "mmio_readback",
    "documentation_note",
    "no_op",
)


@dataclass(frozen=True)
class NoopHarnessOptimizerBackend:
    source: str = "noop"

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": f"{task.get('run_id') or 'unknown'}:noop",
            "created_at": utc_timestamp(),
            "source": self.source,
            "status": "no_op",
            "actions": [],
            "evidence_refs": [],
            "rationale": (
                "No harness optimizer backend is configured; this proposal records "
                "a valid no-op placeholder for downstream validation."
            ),
        }


@dataclass(frozen=True)
class LlmHarnessOptimizerBackend:
    model: str
    transport: HarnessLlmTransport | None = None
    source: str = "llm"
    temperature: float = 0.0
    max_repair_attempts: int = 1
    sample_limit: int = 5

    @classmethod
    def from_env(cls) -> "LlmHarnessOptimizerBackend":
        model = os.getenv("HARNESS_OPTIMIZER_LLM_MODEL", "gpt-4.1-mini")
        repairs = int(os.getenv("HARNESS_OPTIMIZER_REPAIR_ATTEMPTS", "1"))
        sample_limit = int(os.getenv("HARNESS_OPTIMIZER_SAMPLE_LIMIT", "5"))
        return cls(
            model=model,
            transport=OpenAICompatibleChatTransport.from_env(),
            max_repair_attempts=repairs,
            sample_limit=sample_limit,
        )

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "LlmHarnessOptimizerBackend requires run_with_context() so prompt "
            "and response artifacts can be materialized."
        )

    def run_with_context(
        self,
        task: dict[str, Any],
        context: HarnessOptimizerContext,
    ) -> dict[str, Any]:
        transport = self.transport or OpenAICompatibleChatTransport.from_env()
        prompt = build_harness_optimizer_prompt(
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
                proposal_kind=PROPOSAL_KIND,
                attempt_index=attempt_index,
            )
            validation = validate_harness_optimization_proposal(proposal, task=task)
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
            "kind": "libafl_bfm_fuzz.harness_optimizer_llm_response",
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
            proposal_kind=PROPOSAL_KIND,
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


@dataclass(frozen=True)
class PromptOnlyHarnessOptimizerBackend:
    source: str = "prompt_only"
    model: str = "prompt-only"
    sample_limit: int = 5

    @classmethod
    def from_env(cls) -> "PromptOnlyHarnessOptimizerBackend":
        return cls(
            model=os.getenv("HARNESS_OPTIMIZER_LLM_MODEL", "prompt-only"),
            sample_limit=int(os.getenv("HARNESS_OPTIMIZER_SAMPLE_LIMIT", "5")),
        )

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "PromptOnlyHarnessOptimizerBackend requires run_with_context() so "
            "the prompt artifact can be materialized."
        )

    def run_with_context(
        self,
        task: dict[str, Any],
        context: HarnessOptimizerContext,
    ) -> dict[str, Any]:
        prompt = build_harness_optimizer_prompt(
            task,
            sample_limit=self.sample_limit,
        )
        write_json(context.paths.optimizer_prompt, prompt)
        response_artifact = {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.harness_optimizer_llm_response",
            "created_at": utc_timestamp(),
            "model": self.model,
            "source": self.source,
            "status": "not_called",
            "attempt_count": 0,
            "repaired": False,
            "attempts": [],
            "reason": (
                "Prompt-only harness optimizer backend wrote the optimizer "
                "prompt for external review and intentionally skipped the LLM "
                "transport call."
            ),
        }
        write_json(context.paths.optimizer_response, response_artifact)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": f"{task.get('run_id') or 'unknown'}:prompt_only",
            "created_at": utc_timestamp(),
            "source": self.source,
            "status": "no_op",
            "actions": [],
            "evidence_refs": [],
            "rationale": (
                "Prompt-only backend generated no optimization actions; use "
                "the prompt artifact with an external LLM or switch to the llm "
                "backend for structured suggestions."
            ),
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
        return proposal


@dataclass(frozen=True)
class NoopHarnessCandidateEvaluationBackend:
    source: str = "noop"

    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        baseline_metrics = baseline_metric_snapshot(task)
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
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
            "reason": (
                "No candidate evaluation backend is configured; this report keeps "
                "the validation framework explicit without running a regression."
            ),
        }


@dataclass(frozen=True)
class HarnessOptimizationAdapter:
    target: str
    paths: HarnessOptimizationPaths
    campaign_evaluation_path: Path
    campaign_manifest_path: Path
    cwd: Path
    optimizer_backend: HarnessOptimizerBackend = NoopHarnessOptimizerBackend()
    candidate_evaluation_backend: HarnessCandidateEvaluationBackend = (
        NoopHarnessCandidateEvaluationBackend()
    )
    plugin_registry: HarnessPluginRegistry | None = None

    def run_task(
        self,
        campaign_evaluation: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        task = build_harness_optimization_task(
            campaign_evaluation=campaign_evaluation,
            campaign_manifest=campaign_manifest,
            campaign_evaluation_path=self.campaign_evaluation_path,
            campaign_manifest_path=self.campaign_manifest_path,
            target=self.target,
            cwd=self.cwd,
            plugin_registry=self.plugin_registry,
        )
        write_json(self.paths.task, task)
        return task

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
        except Exception as exc:  # noqa: BLE001 - decision stage rejects invalid proposal
            proposal = {
                "schema_version": 1,
                "kind": PROPOSAL_KIND,
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
            raw_type = type(proposal).__name__
            proposal = {
                "schema_version": 1,
                "kind": PROPOSAL_KIND,
                "proposal_id": f"{task.get('run_id') or 'unknown'}:invalid",
                "created_at": utc_timestamp(),
                "source": type(self.optimizer_backend).__name__,
                "status": "invalid",
                "actions": [],
                "evidence_refs": [],
                "rationale": "optimizer backend returned a non-object proposal",
                "raw_type": raw_type,
            }
        write_json(self.paths.proposal, proposal)
        return proposal

    def run_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        decision = build_harness_optimization_decision(
            task=task,
            proposal=proposal,
            plugin_registry=self.plugin_registry,
        )
        write_json(self.paths.decision, decision)
        return decision

    def run_advice_report(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        report = build_harness_optimization_advice_report(
            task=task,
            proposal=proposal,
            decision=decision,
            paths=self.paths,
        )
        write_json(self.paths.advice_report, report)
        return report

    def run_apply(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        patch, candidate_manifest = build_harness_optimization_patch(
            task=task,
            proposal=proposal,
            decision=decision,
            paths=self.paths,
            plugin_registry=self.plugin_registry,
        )
        write_json(self.paths.patch, patch)
        write_json(self.paths.candidate_manifest, candidate_manifest)
        return {
            "harness_optimization_patch": patch,
            "harness_optimization_candidate_manifest": candidate_manifest,
        }

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
        except Exception as exc:  # noqa: BLE001 - final decision rejects failed validation
            evaluation = candidate_evaluation_error(
                task=task,
                proposal=proposal,
                source=type(self.candidate_evaluation_backend).__name__,
                error_type=type(exc).__name__,
                message=str(exc),
            )
        evaluation = normalize_candidate_evaluation(
            evaluation,
            task=task,
            proposal=proposal,
            source=type(self.candidate_evaluation_backend).__name__,
        )
        write_json(self.paths.candidate_evaluation, evaluation)
        return evaluation

    def run_metric_delta(
        self,
        task: dict[str, Any],
        candidate_evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        delta = build_harness_optimization_metric_delta(
            task=task,
            candidate_evaluation=candidate_evaluation,
        )
        write_json(self.paths.metric_delta, delta)
        return delta

    def run_final_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        schema_decision: dict[str, Any],
        patch: dict[str, Any],
        candidate_evaluation: dict[str, Any],
        metric_delta: dict[str, Any],
    ) -> dict[str, Any]:
        decision = build_harness_optimization_final_decision(
            task=task,
            proposal=proposal,
            schema_decision=schema_decision,
            patch=patch,
            candidate_evaluation=candidate_evaluation,
            metric_delta=metric_delta,
        )
        write_json(self.paths.final_decision, decision)
        return decision


def build_harness_optimization_task(
    *,
    campaign_evaluation: dict[str, Any],
    campaign_manifest: dict[str, Any],
    campaign_evaluation_path: Path,
    campaign_manifest_path: Path,
    target: str,
    cwd: Path,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = plugin_registry or default_harness_plugin_registry()
    harness_trace = mapping(campaign_evaluation.get("harness_trace"))
    trace_artifacts = mapping(harness_trace.get("artifacts"))
    harness_evaluation_path = _resolved_path(
        trace_artifacts.get("harness_evaluation"),
        cwd,
    )
    llm_dataset_path = _resolved_path(
        trace_artifacts.get("llm_optimization_dataset"),
        cwd,
    )
    campaign_rollup_path = _resolved_path(
        trace_artifacts.get("campaign_trace_rollup"),
        cwd,
    )
    action_effect_report_path = _resolved_path(
        trace_artifacts.get("candidate_action_effect_report")
        or mapping(campaign_manifest.get("artifacts")).get(
            "candidate_action_effect_report"
        ),
        cwd,
    )
    harness_evaluation = read_json_object(harness_evaluation_path)
    campaign_rollup = read_json_object(campaign_rollup_path)
    action_effect_report = read_json_object(action_effect_report_path)
    evidence_index = evidence_index_for(harness_evaluation)
    return {
        "schema_version": 1,
        "kind": TASK_KIND,
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
            **_optional_path("harness_evaluation", harness_evaluation_path),
            **_optional_path("llm_optimization_dataset", llm_dataset_path),
            **_optional_path("campaign_trace_rollup", campaign_rollup_path),
            **_optional_path("candidate_action_effect_report", action_effect_report_path),
        },
        "summary": {
            "campaign": mapping(campaign_evaluation.get("summary")),
            "harness": mapping(harness_evaluation.get("summary"))
            or mapping(harness_trace.get("summary")),
            "campaign_rollup": mapping(campaign_rollup.get("summary"))
            or mapping(harness_trace.get("campaign_rollup", {})).get("summary", {}),
            "llm_sample_count": count_jsonl_items(llm_dataset_path),
            "action_effect": mapping(action_effect_report.get("summary")),
        },
        "optimization_hints": mapping(
            harness_evaluation.get("optimization_hints")
        )
        or mapping(harness_trace.get("optimization_hints")),
        "trace_quality": mapping(harness_evaluation.get("trace_quality")),
        "failure_clusters": list_value(harness_evaluation.get("failure_clusters"))[:5],
        "slowest_records": list_value(harness_evaluation.get("slowest_records"))[:5],
        "coverage_trends": list_value(campaign_rollup.get("coverage_trends"))[:10],
        "failure_trends": list_value(campaign_rollup.get("failure_trends"))[:10],
        "action_effect_report": action_effect_report if action_effect_report else {},
        "evidence_index": evidence_index,
        "constraints": {
            "allowed_action_types": list(registry.allowed_action_types()),
            "safe_sandbox_action_types": list(registry.safe_sandbox_action_types()),
            "safe_action_dsl": registry.safe_action_dsl_schema(),
            "plugin_registry": plugin_registry_to_json(registry),
            "plugin_validation": plugin_registry_validation_json(registry),
            "plugin_provenance": plugin_registry_provenance_json(registry),
            "requires_evidence_refs": True,
            "application_mode": "proposal_only_by_default",
            "default_validation": "schema_only",
            "sandbox_apply": "explicit_profile_only",
            "mainline_apply": False,
        },
        "objective": (
            "Generate a schema-valid harness optimization proposal grounded in "
            "the supplied connector spans, cases, directives, coverage trends, "
            "and failure clusters. The proposal must not assume that changes "
            "are automatically applied."
        ),
    }


def build_harness_optimizer_prompt(
    task: dict[str, Any],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    artifacts = mapping(task.get("artifacts"))
    dataset_path = _path_or_none(artifacts.get("llm_optimization_dataset"))
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
    schema = optimizer_proposal_schema_hint(task)
    system = (
        "You are a hardware verification harness optimizer. Return only JSON. "
        "Generate safe, sandbox-only harness optimization proposals grounded in "
        "the provided evidence. Do not propose source-code mainline edits."
    )
    user = json.dumps(
        {
            "objective": task.get("objective"),
            "proposal_schema": schema,
            "context": context,
        },
        indent=2,
        sort_keys=True,
    )
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_optimizer_prompt",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "schema_hint": schema,
        "context": context,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }


def optimizer_proposal_schema_hint(task: dict[str, Any]) -> dict[str, Any]:
    return _optimizer_proposal_schema_hint(
        task,
        proposal_kind=PROPOSAL_KIND,
        default_allowed_action_types=ALLOWED_ACTION_TYPES,
        default_safe_action_types=SAFE_SANDBOX_ACTION_TYPES,
    )


def safe_action_dsl_schema(
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    return _safe_action_dsl_schema(plugin_registry)


def builtin_safe_action_dsl_schema() -> dict[str, Any]:
    return _builtin_safe_action_dsl_schema()


def build_harness_optimization_decision(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    validation = validate_harness_optimization_proposal(
        proposal,
        task=task,
        plugin_registry=plugin_registry,
    )
    decision = "accepted" if validation["valid"] else "rejected"
    return {
        "schema_version": 1,
        "kind": DECISION_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "decision": decision,
        "application_status": "not_applied",
        "validation": validation,
        "summary": {
            "action_count": len(list_value(proposal.get("actions"))),
            "accepted_action_count": (
                len(list_value(proposal.get("actions"))) if validation["valid"] else 0
            ),
            "sandbox_validation": "not_configured",
        },
        "reason": (
            "proposal schema is valid; sandbox apply and regression validation are "
            "not part of phase one"
            if validation["valid"]
            else "proposal rejected by schema validation"
        ),
    }


def build_harness_optimization_advice_report(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    decision: dict[str, Any],
    paths: HarnessOptimizationPaths | None = None,
) -> dict[str, Any]:
    actions = [
        item for item in list_value(proposal.get("actions")) if isinstance(item, dict)
    ]
    accepted = decision.get("decision") == "accepted"
    recommended_actions = [
        advice_action_summary(action, task, proposal)
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
    suggested_candidate_controls = (
        {
            "matched_baseline": True,
            "paired_repeats": 3,
            "attribution_mode": "all_actions",
            "min_improved_metrics": 1,
            "max_regressed_metrics": 0,
            "max_flaky_metrics": 0,
        }
        if recommended_actions
        else {}
    )
    return {
        "schema_version": 1,
        "kind": ADVICE_REPORT_KIND,
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
                "campaign_with_evaluation_and_optimization_real_validation"
                if recommended_actions
                else None
            ),
            "suggested_candidate_backend": "real" if recommended_actions else None,
            "suggested_candidate_controls": suggested_candidate_controls,
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


def advice_action_summary(
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


def build_harness_optimization_patch(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    decision: dict[str, Any],
    paths: HarnessOptimizationPaths,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = plugin_registry or default_harness_plugin_registry()
    safe_action_types = set(registry.safe_sandbox_action_types())
    unsafe_action_types = set(registry.unsafe_action_types())
    candidate_id = candidate_id_for(task, proposal)
    sandbox_dir = paths.sandbox_dir / candidate_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    applied_actions: list[dict[str, Any]] = []
    skipped_actions: list[dict[str, Any]] = []
    schema_accepted = decision.get("decision") == "accepted"
    actions = list_value(proposal.get("actions"))
    if not schema_accepted:
        skipped_actions = [
            skipped_action(action, reason="schema_decision_rejected")
            for action in actions
        ]
    elif proposal.get("status") == "no_op" or not actions:
        skipped_actions = [
            skipped_action(action, reason="no_op_proposal") for action in actions
        ]
    else:
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                skipped_actions.append(
                    {
                        "action_index": index,
                        "reason": "action_is_not_object",
                    }
                )
                continue
            action_type = str(action.get("action_type") or "")
            if action_type not in safe_action_types:
                skipped_actions.append(
                    skipped_action(action, reason="unsafe_action_type")
                )
                continue
            if action_type == "no_op":
                skipped_actions.append(skipped_action(action, reason="no_op_action"))
                continue
            artifact_path = (
                sandbox_dir
                / f"{index:02d}_{safe_slug(str(action.get('action_id') or index))}_"
                f"{safe_slug(action_type)}.json"
            )
            artifact = candidate_action_artifact(
                task=task,
                proposal=proposal,
                action=action,
                candidate_id=candidate_id,
            )
            write_json(artifact_path, artifact)
            applied_actions.append(
                {
                    "action_id": action.get("action_id"),
                    "action_type": action_type,
                    "artifact_role": f"candidate_{action_type}",
                    "artifact_path": str(artifact_path),
                    "evidence_refs": list_value(action.get("evidence_refs"))
                    or list_value(proposal.get("evidence_refs")),
                }
            )

    patch_status = patch_status_for(
        schema_accepted=schema_accepted,
        proposal=proposal,
        applied_actions=applied_actions,
        skipped_actions=skipped_actions,
    )
    patch = {
        "schema_version": 1,
        "kind": PATCH_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_id,
        "status": patch_status,
        "sandbox_dir": str(sandbox_dir),
        "safety": {
            "policy": "sandbox_artifacts_only",
            "mainline_modified": False,
            "safe_action_types": sorted(safe_action_types),
            "unsafe_action_types": sorted(unsafe_action_types),
            "plugin_registry": plugin_registry_to_json(registry),
            "plugin_validation": plugin_registry_validation_json(registry),
            "plugin_provenance": plugin_registry_provenance_json(registry),
        },
        "applied_actions": applied_actions,
        "skipped_actions": skipped_actions,
        "summary": {
            "action_count": len(actions),
            "applied_action_count": len(applied_actions),
            "skipped_action_count": len(skipped_actions),
            "unsafe_skipped_count": sum(
                1
                for action in skipped_actions
                if action.get("reason") == "unsafe_action_type"
            ),
        },
    }
    candidate_manifest = {
        "schema_version": 1,
        "kind": CANDIDATE_MANIFEST_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_id,
        "status": patch_status,
        "sandbox_dir": str(sandbox_dir),
        "artifacts": {
            "harness_optimization_patch": str(paths.patch),
            "candidate_artifacts": [
                action["artifact_path"] for action in applied_actions
            ],
        },
        "candidate_artifacts": applied_actions,
        "skipped_actions": skipped_actions,
        "safety": patch["safety"],
    }
    return patch, candidate_manifest


def build_harness_optimization_metric_delta(
    *,
    task: dict[str, Any],
    candidate_evaluation: dict[str, Any],
) -> dict[str, Any]:
    return _build_candidate_metric_delta(
        task=task,
        candidate_evaluation=candidate_evaluation,
        kind=METRIC_DELTA_KIND,
    )


def build_harness_optimization_final_decision(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    schema_decision: dict[str, Any],
    patch: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    metric_delta: dict[str, Any],
) -> dict[str, Any]:
    return _build_candidate_final_decision(
        task=task,
        proposal=proposal,
        schema_decision=schema_decision,
        patch=patch,
        candidate_evaluation=candidate_evaluation,
        metric_delta=metric_delta,
        kind=FINAL_DECISION_KIND,
    )


def validate_harness_optimization_proposal(
    proposal: dict[str, Any],
    *,
    task: dict[str, Any],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = plugin_registry or default_harness_plugin_registry()
    return _validate_harness_optimization_proposal(
        proposal,
        task=task,
        proposal_kind=PROPOSAL_KIND,
        plugin_registry=registry,
        default_allowed_action_types=ALLOWED_ACTION_TYPES,
    )


def normalize_candidate_evaluation(
    value: Any,
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    return _normalize_candidate_evaluation(
        value,
        task=task,
        proposal=proposal,
        source=source,
        candidate_evaluation_kind=CANDIDATE_EVALUATION_KIND,
    )


def candidate_evaluation_error(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return _candidate_evaluation_error(
        task=task,
        proposal=proposal,
        source=source,
        error_type=error_type,
        message=message,
        candidate_evaluation_kind=CANDIDATE_EVALUATION_KIND,
    )


def action_payload_errors(
    action_type: str,
    payload: dict[str, Any],
    *,
    path: str,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> list[dict[str, str]]:
    return _action_payload_errors(
        action_type,
        payload,
        path=path,
        plugin_registry=plugin_registry,
    )


def default_harness_plugin_registry() -> HarnessPluginRegistry:
    schema = builtin_safe_action_dsl_schema()
    return HarnessPluginRegistry(
        action_plugins={
            "mutation_directive_update": HarnessActionPlugin(
                action_type="mutation_directive_update",
                adapter_kind=(
                    "libafl_bfm_fuzz.harness_candidate_mutation_directive_updates"
                ),
                artifact_role="candidate_mutation_directive_updates",
                make_var="HARNESS_MUTATION_DIRECTIVE_UPDATE_CONFIG",
            ),
            "stimulus_generation_hint": HarnessActionPlugin(
                action_type="stimulus_generation_hint",
                adapter_kind="libafl_bfm_fuzz.harness_candidate_stimulus_hint_config",
                artifact_role="candidate_stimulus_hint_config",
                make_var="HARNESS_STIMULUS_HINT_CONFIG",
            ),
            "replay_probe": HarnessActionPlugin(
                action_type="replay_probe",
                payload_required=True,
                dsl_schema=schema["replay_probe"],
                payload_validator=lambda payload, path: replay_probe_payload_errors(
                    payload,
                    path=path,
                ),
                adapter_kind="libafl_bfm_fuzz.harness_candidate_replay_probe_config",
                artifact_role="candidate_replay_probe_config",
                make_var="HARNESS_REPLAY_PROBE_CONFIG",
                runtime_action=True,
            ),
            "scoreboard_check": HarnessActionPlugin(
                action_type="scoreboard_check",
                payload_required=True,
                dsl_schema=schema["scoreboard_check"],
                payload_validator=lambda payload, path: scoreboard_check_payload_errors(
                    payload,
                    path=path,
                ),
                adapter_kind=(
                    "libafl_bfm_fuzz.harness_candidate_scoreboard_check_config"
                ),
                artifact_role="candidate_scoreboard_check_config",
                make_var="HARNESS_SCOREBOARD_CHECK_CONFIG",
                runtime_action=True,
            ),
            "ref_model_patch": HarnessActionPlugin(
                action_type="ref_model_patch",
                safe_for_sandbox=False,
            ),
            "coverage_feedback_tuning": HarnessActionPlugin(
                action_type="coverage_feedback_tuning",
                payload_required=True,
                dsl_schema=schema["coverage_feedback_tuning"],
                payload_validator=(
                    lambda payload, path: coverage_feedback_tuning_payload_errors(
                        payload,
                        path=path,
                    )
                ),
                adapter_kind=(
                    "libafl_bfm_fuzz."
                    "harness_candidate_coverage_feedback_tuning_config"
                ),
                artifact_role="candidate_coverage_feedback_tuning_config",
                make_var="HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG",
                runtime_action=True,
            ),
            "mmio_readback": HarnessActionPlugin(
                action_type="mmio_readback",
                payload_required=True,
                dsl_schema=schema["mmio_readback"],
                payload_validator=lambda payload, path: mmio_readback_payload_errors(
                    payload,
                    path=path,
                ),
                adapter_kind="libafl_bfm_fuzz.harness_candidate_mmio_readback_config",
                artifact_role="candidate_mmio_readback_config",
                make_var="HARNESS_MMIO_READBACK_CONFIG",
                runtime_action=True,
            ),
            "documentation_note": HarnessActionPlugin(
                action_type="documentation_note",
                adapter_kind="libafl_bfm_fuzz.harness_candidate_documentation_notes",
                artifact_role="candidate_documentation_notes",
                make_var="HARNESS_DOCUMENTATION_NOTES",
            ),
            "no_op": HarnessActionPlugin(action_type="no_op"),
        }
    )


def candidate_action_artifact(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    action: dict[str, Any],
    candidate_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_optimization_candidate_action",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_id,
        "action": action,
        "evidence_refs": list_value(action.get("evidence_refs"))
        or list_value(proposal.get("evidence_refs")),
        "safety": {
            "application": "sandbox_artifact",
            "mainline_modified": False,
        },
    }


def skipped_action(action: Any, *, reason: str) -> dict[str, Any]:
    if not isinstance(action, dict):
        return {"reason": reason}
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "reason": reason,
    }


def patch_status_for(
    *,
    schema_accepted: bool,
    proposal: dict[str, Any],
    applied_actions: list[dict[str, Any]],
    skipped_actions: list[dict[str, Any]],
) -> str:
    if not schema_accepted:
        return "skipped"
    if proposal.get("status") == "no_op":
        return "no_op"
    if applied_actions and skipped_actions:
        return "partial"
    if applied_actions:
        return "applied"
    return "skipped"


def count_jsonl_items(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                count += 1
    return count


def _resolved_path(value: object, cwd: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return cwd / path


def _path_or_none(value: object) -> Path | None:
    if not isinstance(value, str | Path) or not str(value):
        return None
    return Path(value)


def _optional_path(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}
