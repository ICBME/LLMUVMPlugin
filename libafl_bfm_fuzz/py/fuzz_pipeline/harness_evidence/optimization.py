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

LOWER_IS_BETTER_METRICS = {
    "failed_record_count",
    "hanging_span_count",
    "malformed_event_line_count",
    "missing_span_id_count",
    "orphan_final_count",
    "orphan_span_count",
    "scoreboard_check_enforced_failure_count",
    "scoreboard_check_failed_count",
    "uncovered_line_count",
}

HIGHER_IS_BETTER_METRICS = {
    "covered_line_count",
    "coverage_percent",
}

INFORMATIONAL_METRICS = {
    "case_count",
    "directive_count",
    "llm_sample_count",
    "record_count",
    "round_count",
}

SCOREBOARD_CHECK_MODES = {
    "actual_equals_expected",
    "record_seen",
    "require_no_error",
    "field_equals",
    "field_range",
}
DSL_PAYLOAD_ACTION_TYPES = {
    "replay_probe",
    "scoreboard_check",
    "coverage_feedback_tuning",
    "mmio_readback",
}


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
    constraints = mapping(task.get("constraints"))
    return {
        "schema_version": 1,
        "kind": PROPOSAL_KIND,
        "required_top_level_fields": [
            "schema_version",
            "kind",
            "proposal_id",
            "status",
            "actions",
            "evidence_refs",
        ],
        "status_values": ["no_op", "proposed"],
        "allowed_action_types": list_value(constraints.get("allowed_action_types"))
        or list(ALLOWED_ACTION_TYPES),
        "safe_sandbox_action_types": list_value(
            constraints.get("safe_sandbox_action_types")
        )
        or list(SAFE_SANDBOX_ACTION_TYPES),
        "safe_action_dsl": mapping(constraints.get("safe_action_dsl"))
        or safe_action_dsl_schema(),
        "evidence_ref_fields": ["span_id", "case_id", "directive_id", "connector"],
    }


def safe_action_dsl_schema(
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    if plugin_registry is not None:
        return plugin_registry.safe_action_dsl_schema()
    return builtin_safe_action_dsl_schema()


def builtin_safe_action_dsl_schema() -> dict[str, Any]:
    return {
        "replay_probe": {
            "payload_fields": {
                "fields": "string or list of case./result. field names",
                "signals": "string or list of DUT signal names",
                "probe": "single case/result field alias",
                "sample_on": "optional sampling point string",
                "max_samples": "optional non-negative integer",
                "case_filter": "optional object for future filtering",
            },
            "requires_any": ["fields", "signals", "probe"],
        },
        "scoreboard_check": {
            "modes": sorted(SCOREBOARD_CHECK_MODES),
            "payload_fields": {
                "mode": "check mode",
                "check": "human readable check description",
                "field": "record/result/case field for field modes",
                "expected": "expected value for field_equals",
                "min": "minimum numeric value for field_range",
                "max": "maximum numeric value for field_range",
                "enforce": "bool, fail candidate when check fails",
            },
        },
        "coverage_feedback_tuning": {
            "payload_fields": {
                "max_gap_count": "optional non-negative integer",
                "directive_weight_multiplier": "optional positive number",
                "prioritize": "optional priority label",
                "gap_type": "optional gap category",
                "directive_source": "optional directive source filter",
                "min_weight": "optional directive weight floor",
                "max_weight": "optional directive weight ceiling",
            },
        },
        "mmio_readback": {
            "payload_fields": {
                "registers": "string or list of symbolic register names",
                "addresses": "integer, hex string, or list of addresses",
                "sample_on": "optional sampling point string",
                "max_reads": "optional non-negative integer per run",
                "case_filter": "optional object matching case fields",
            },
            "requires_any": ["registers", "addresses"],
        },
    }


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
    baseline = numeric_metrics(
        mapping(candidate_evaluation.get("baseline_metrics"))
        or baseline_metric_snapshot(task)
    )
    candidate = numeric_metrics(mapping(candidate_evaluation.get("candidate_metrics")))
    comparisons = []
    for name in sorted(set(baseline) | set(candidate)):
        base_value = baseline.get(name)
        candidate_value = candidate.get(name)
        role = metric_role(name)
        gates_acceptance = metric_gates_acceptance(name)
        if base_value is None or candidate_value is None:
            comparisons.append(
                {
                    "metric": name,
                    "role": role,
                    "gates_acceptance": gates_acceptance,
                    "baseline": base_value,
                    "candidate": candidate_value,
                    "delta": None,
                    "direction": "not_comparable",
                }
            )
            continue
        delta = candidate_value - base_value
        comparisons.append(
            {
                "metric": name,
                "role": role,
                "gates_acceptance": gates_acceptance,
                "baseline": base_value,
                "candidate": candidate_value,
                "delta": delta,
                "direction": metric_direction(name, delta),
            }
        )
    improved = sum(1 for item in comparisons if item["direction"] == "improved")
    regressed = sum(1 for item in comparisons if item["direction"] == "regressed")
    unchanged = sum(1 for item in comparisons if item["direction"] == "unchanged")
    gateable_improved = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "improved"
    )
    gateable_regressed = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "regressed"
    )
    gateable_unchanged = sum(
        1
        for item in comparisons
        if item["gates_acceptance"] and item["direction"] == "unchanged"
    )
    informational_changed = sum(
        1
        for item in comparisons
        if not item["gates_acceptance"] and item["direction"] == "changed"
    )
    return {
        "schema_version": 1,
        "kind": METRIC_DELTA_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "candidate_status": candidate_evaluation.get("status"),
        "comparisons": comparisons,
        "summary": {
            "metric_count": len(comparisons),
            "improved_metric_count": improved,
            "regressed_metric_count": regressed,
            "unchanged_metric_count": unchanged,
            "gateable_metric_count": sum(
                1 for item in comparisons if item["gates_acceptance"]
            ),
            "gateable_improved_metric_count": gateable_improved,
            "gateable_regressed_metric_count": gateable_regressed,
            "gateable_unchanged_metric_count": gateable_unchanged,
            "informational_metric_count": sum(
                1 for item in comparisons if not item["gates_acceptance"]
            ),
            "informational_changed_metric_count": informational_changed,
            "not_comparable_metric_count": sum(
                1 for item in comparisons if item["direction"] == "not_comparable"
            ),
        },
    }


def build_harness_optimization_final_decision(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    schema_decision: dict[str, Any],
    patch: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    metric_delta: dict[str, Any],
) -> dict[str, Any]:
    delta_summary = mapping(metric_delta.get("summary"))
    patch_summary = mapping(patch.get("summary"))
    candidate_status = candidate_evaluation.get("status")
    thresholds = final_decision_thresholds(candidate_evaluation, metric_delta)
    regressed_metric_count = summary_metric_count(
        delta_summary,
        "gateable_regressed_metric_count",
        "regressed_metric_count",
    )
    improved_metric_count = summary_metric_count(
        delta_summary,
        "gateable_improved_metric_count",
        "improved_metric_count",
    )
    stability_summary = mapping(candidate_evaluation.get("stability_summary"))
    flaky_metric_count = int_value(stability_summary.get("flaky_metric_count"))
    if schema_decision.get("decision") != "accepted":
        decision = "rejected"
        reason = "schema_decision_rejected"
    elif proposal.get("status") == "no_op" or patch.get("status") == "no_op":
        decision = "no_op"
        reason = "proposal_has_no_applicable_actions"
    elif int_value(patch_summary.get("applied_action_count")) == 0:
        decision = "rejected"
        reason = "no_safe_actions_applied"
    elif candidate_status in {"error", "failed"}:
        decision = "rejected"
        reason = "candidate_validation_failed"
    elif candidate_status == "not_run":
        decision = "rejected"
        reason = "candidate_validation_not_run"
    elif regressed_metric_count > thresholds["max_regressed_metric_count"]:
        decision = "rejected"
        reason = "candidate_metric_regression"
    elif improved_metric_count < thresholds["min_improved_metric_count"]:
        decision = "rejected"
        reason = "candidate_metric_improvement_below_threshold"
    elif flaky_metric_count > thresholds["max_flaky_metric_count"]:
        decision = "rejected"
        reason = "candidate_stability_below_threshold"
    elif candidate_status in set(thresholds["accepted_candidate_statuses"]):
        decision = "accepted_for_review"
        reason = "candidate_validation_passed_without_regressions"
    else:
        decision = "rejected"
        reason = "candidate_validation_status_not_accepted"
    return {
        "schema_version": 1,
        "kind": FINAL_DECISION_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": patch.get("candidate_id"),
        "decision": decision,
        "reason": reason,
        "application_status": "not_applied",
        "safety": {
            "mainline_modified": False,
            "sandbox_dir": patch.get("sandbox_dir"),
        },
        "summary": {
            "schema_decision": schema_decision.get("decision"),
            "patch_status": patch.get("status"),
            "candidate_status": candidate_status,
            "applied_action_count": int_value(
                patch_summary.get("applied_action_count")
            ),
            "improved_metric_count": int_value(
                delta_summary.get("improved_metric_count")
            ),
            "regressed_metric_count": int_value(
                delta_summary.get("regressed_metric_count")
            ),
            "gateable_improved_metric_count": improved_metric_count,
            "gateable_regressed_metric_count": regressed_metric_count,
            "informational_changed_metric_count": int_value(
                delta_summary.get("informational_changed_metric_count")
            ),
            "flaky_metric_count": flaky_metric_count,
            "acceptance_thresholds": thresholds,
        },
    }


def validate_harness_optimization_proposal(
    proposal: dict[str, Any],
    *,
    task: dict[str, Any],
    plugin_registry: HarnessPluginRegistry | None = None,
) -> dict[str, Any]:
    registry = plugin_registry or default_harness_plugin_registry()
    errors: list[dict[str, Any]] = []
    if proposal.get("kind") != PROPOSAL_KIND:
        errors.append({"path": "kind", "message": f"expected {PROPOSAL_KIND!r}"})
    if proposal.get("schema_version") != 1:
        errors.append(
            {"path": "schema_version", "message": "expected schema_version 1"}
        )
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id:
        errors.append({"path": "proposal_id", "message": "expected non-empty string"})
    status = proposal.get("status")
    if status not in {"no_op", "proposed"}:
        errors.append({"path": "status", "message": "expected 'no_op' or 'proposed'"})
    actions = proposal.get("actions")
    if not isinstance(actions, list):
        errors.append({"path": "actions", "message": "expected list"})
        actions = []
    if status == "proposed" and not actions:
        errors.append(
            {
                "path": "actions",
                "message": "proposed status requires at least one action",
            }
        )

    allowed = set(
        list_value(mapping(task.get("constraints")).get("allowed_action_types"))
    )
    if not allowed:
        allowed = set(registry.allowed_action_types())
    dsl_payload_action_types = set(registry.dsl_payload_action_types())
    evidence_index = mapping(task.get("evidence_index"))
    proposal_refs = list_value(proposal.get("evidence_refs"))
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(
                {"path": f"actions[{index}]", "message": "expected action object"}
            )
            continue
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            errors.append(
                {
                    "path": f"actions[{index}].action_id",
                    "message": "expected non-empty string",
                }
            )
        action_type = action.get("action_type")
        if action_type not in allowed:
            errors.append(
                {
                    "path": f"actions[{index}].action_type",
                    "message": f"unsupported action type {action_type!r}",
                }
            )
        payload = action.get("payload")
        if action_type in dsl_payload_action_types and not isinstance(payload, dict):
            errors.append(
                {
                    "path": f"actions[{index}].payload",
                    "message": f"{action_type} payload is required",
                }
            )
        elif payload is not None and not isinstance(payload, dict):
            errors.append(
                {
                    "path": f"actions[{index}].payload",
                    "message": "expected payload object",
                }
            )
        elif isinstance(payload, dict):
            errors.extend(
                action_payload_errors(
                    str(action_type),
                    payload,
                    path=f"actions[{index}].payload",
                    plugin_registry=registry,
                )
            )
        action_refs = list_value(action.get("evidence_refs")) or proposal_refs
        if status == "proposed" and not action_refs:
            errors.append(
                {
                    "path": f"actions[{index}].evidence_refs",
                    "message": "proposed actions require evidence_refs",
                }
            )
        for ref_index, ref in enumerate(action_refs):
            ref_error = evidence_ref_error(ref, evidence_index)
            if ref_error is not None:
                errors.append(
                    {
                        "path": f"actions[{index}].evidence_refs[{ref_index}]",
                        "message": ref_error,
                    }
                )

    return {
        "valid": not errors,
        "error_count": len(errors),
        "errors": errors,
        "allowed_action_types": sorted(allowed),
    }


def action_payload_errors(
    action_type: str,
    payload: dict[str, Any],
    *,
    path: str,
    plugin_registry: HarnessPluginRegistry | None = None,
) -> list[dict[str, str]]:
    if plugin_registry is not None:
        return plugin_registry.action_payload_errors(action_type, payload, path=path)
    if action_type == "replay_probe":
        return replay_probe_payload_errors(payload, path=path)
    if action_type == "scoreboard_check":
        return scoreboard_check_payload_errors(payload, path=path)
    if action_type == "coverage_feedback_tuning":
        return coverage_feedback_tuning_payload_errors(payload, path=path)
    if action_type == "mmio_readback":
        return mmio_readback_payload_errors(payload, path=path)
    return []


def replay_probe_payload_errors(
    payload: dict[str, Any],
    *,
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    fields = payload.get("fields")
    signals = payload.get("signals")
    probe = payload.get("probe")
    if fields is None and signals is None and probe is None:
        errors.append(
            {
                "path": path,
                "message": "replay_probe payload requires fields, signals, or probe",
            }
        )
    if fields is not None and not is_string_or_string_list(fields):
        errors.append({"path": f"{path}.fields", "message": "expected string or list"})
    if signals is not None and not is_string_or_string_list(signals):
        errors.append({"path": f"{path}.signals", "message": "expected string or list"})
    if probe is not None and not isinstance(probe, str):
        errors.append({"path": f"{path}.probe", "message": "expected string"})
    if "sample_on" in payload and not isinstance(payload["sample_on"], str):
        errors.append({"path": f"{path}.sample_on", "message": "expected string"})
    if "max_samples" in payload and not non_negative_int(payload["max_samples"]):
        errors.append(
            {"path": f"{path}.max_samples", "message": "expected non-negative integer"}
        )
    if "case_filter" in payload and not isinstance(payload["case_filter"], dict):
        errors.append({"path": f"{path}.case_filter", "message": "expected object"})
    return errors


def scoreboard_check_payload_errors(
    payload: dict[str, Any],
    *,
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    mode = str(payload.get("mode") or "actual_equals_expected")
    if "mode" in payload and mode not in SCOREBOARD_CHECK_MODES:
        errors.append(
            {
                "path": f"{path}.mode",
                "message": f"expected one of {sorted(SCOREBOARD_CHECK_MODES)}",
            }
        )
    if "mode" not in payload and "check" not in payload:
        errors.append({"path": path, "message": "requires check or mode"})
    if "check" in payload and not isinstance(payload["check"], str):
        errors.append({"path": f"{path}.check", "message": "expected string"})
    if "enforce" in payload and not isinstance(payload["enforce"], bool):
        errors.append({"path": f"{path}.enforce", "message": "expected bool"})
    if mode in {"field_equals", "field_range"}:
        if not isinstance(payload.get("field"), str):
            errors.append({"path": f"{path}.field", "message": "expected string"})
    if mode == "field_equals" and "expected" not in payload:
        errors.append({"path": f"{path}.expected", "message": "required"})
    if mode == "field_range":
        if "min" not in payload and "max" not in payload:
            errors.append({"path": path, "message": "field_range requires min or max"})
        for key in ("min", "max"):
            if key in payload and number_value(payload[key]) is None:
                errors.append({"path": f"{path}.{key}", "message": "expected number"})
    return errors


def coverage_feedback_tuning_payload_errors(
    payload: dict[str, Any],
    *,
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    known_keys = {
        "max_gap_count",
        "directive_weight_multiplier",
        "prioritize",
        "gap_type",
        "directive_source",
        "min_weight",
        "max_weight",
    }
    if not any(key in payload for key in known_keys):
        errors.append({"path": path, "message": "requires at least one tuning field"})
    if "max_gap_count" in payload and not non_negative_int(payload["max_gap_count"]):
        errors.append(
            {
                "path": f"{path}.max_gap_count",
                "message": "expected non-negative integer",
            }
        )
    if "directive_weight_multiplier" in payload and not positive_number(
        payload["directive_weight_multiplier"]
    ):
        errors.append(
            {
                "path": f"{path}.directive_weight_multiplier",
                "message": "expected positive number",
            }
        )
    for key in ("prioritize", "gap_type", "directive_source"):
        if key in payload and not isinstance(payload[key], str):
            errors.append({"path": f"{path}.{key}", "message": "expected string"})
    for key in ("min_weight", "max_weight"):
        if key in payload and number_value(payload[key]) is None:
            errors.append({"path": f"{path}.{key}", "message": "expected number"})
    if (
        "min_weight" in payload
        and "max_weight" in payload
        and number_value(payload["min_weight"]) is not None
        and number_value(payload["max_weight"]) is not None
        and number_value(payload["min_weight"]) > number_value(payload["max_weight"])
    ):
        errors.append(
            {
                "path": path,
                "message": "min_weight must be <= max_weight",
            }
        )
    return errors


def mmio_readback_payload_errors(
    payload: dict[str, Any],
    *,
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    registers = payload.get("registers")
    addresses = payload.get("addresses")
    if registers is None and addresses is None:
        errors.append(
            {"path": path, "message": "mmio_readback requires registers or addresses"}
        )
    if registers is not None and not is_string_or_string_list(registers):
        errors.append(
            {"path": f"{path}.registers", "message": "expected string or list"}
        )
    if addresses is not None and not is_address_or_address_list(addresses):
        errors.append(
            {
                "path": f"{path}.addresses",
                "message": "expected integer, hex string, or list",
            }
        )
    if "sample_on" in payload and not isinstance(payload["sample_on"], str):
        errors.append({"path": f"{path}.sample_on", "message": "expected string"})
    if "max_reads" in payload and not non_negative_int(payload["max_reads"]):
        errors.append(
            {"path": f"{path}.max_reads", "message": "expected non-negative integer"}
        )
    if "case_filter" in payload and not isinstance(payload["case_filter"], dict):
        errors.append({"path": f"{path}.case_filter", "message": "expected object"})
    return errors


def is_string_or_string_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def is_address_or_address_list(value: Any) -> bool:
    if isinstance(value, list):
        return all(is_address_value(item) for item in value)
    return is_address_value(value)


def is_address_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        try:
            return int(value, 0) >= 0
        except ValueError:
            return False
    return False


def non_negative_int(value: Any) -> bool:
    number = number_value(value)
    return (
        number is not None
        and int(number) == number
        and number >= 0
        and not isinstance(value, bool)
    )


def positive_number(value: Any) -> bool:
    number = number_value(value)
    return number is not None and number > 0


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


def evidence_index_for(harness_evaluation: dict[str, Any]) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {
        "span_ids": set(),
        "case_ids": set(),
        "directive_ids": set(),
        "connectors": set(),
    }
    for section in ("failed_records", "slowest_records"):
        for record in list_value(harness_evaluation.get(section)):
            collect_record_evidence(record, values)
    for cluster in list_value(harness_evaluation.get("failure_clusters")):
        connector = cluster.get("connector")
        if connector is not None:
            values["connectors"].add(str(connector))
        for record in list_value(cluster.get("examples")):
            collect_record_evidence(record, values)
    for item in list_value(harness_evaluation.get("case_summary")):
        case_id = item.get("case_id")
        if case_id is not None:
            values["case_ids"].add(str(case_id))
    for item in list_value(harness_evaluation.get("directive_summary")):
        directive_id = item.get("directive_id")
        if directive_id is not None:
            values["directive_ids"].add(str(directive_id))
    return {key: sorted(items) for key, items in values.items()}


def collect_record_evidence(record: Any, values: dict[str, set[str]]) -> None:
    if not isinstance(record, dict):
        return
    span_id = record.get("span_id")
    if span_id is not None:
        values["span_ids"].add(str(span_id))
    case_id = record.get("case_id")
    if case_id is not None:
        values["case_ids"].add(str(case_id))
    directive_id = record.get("directive_id")
    if directive_id is not None:
        values["directive_ids"].add(str(directive_id))
    connector = record.get("connector")
    if connector is not None:
        values["connectors"].add(str(connector))


def evidence_ref_error(ref: Any, evidence_index: dict[str, Any]) -> str | None:
    if not isinstance(ref, dict):
        return "expected evidence ref object"
    allowed_fields = {
        "span_id": "span_ids",
        "case_id": "case_ids",
        "directive_id": "directive_ids",
        "connector": "connectors",
    }
    present = [
        (field, str(ref[field]))
        for field in allowed_fields
        if ref.get(field) is not None
    ]
    if not present:
        return "expected one of span_id, case_id, directive_id, or connector"
    for field, value in present:
        known = set(str(item) for item in list_value(evidence_index.get(allowed_fields[field])))
        if known and value not in known:
            return f"unknown {field} {value!r}"
    return None


def final_decision_thresholds(
    candidate_evaluation: dict[str, Any],
    metric_delta: dict[str, Any],
) -> dict[str, Any]:
    raw = mapping(candidate_evaluation.get("acceptance_thresholds")) or mapping(
        metric_delta.get("acceptance_thresholds")
    )
    statuses = [
        str(item)
        for item in list_value(raw.get("accepted_candidate_statuses"))
        if item is not None
    ]
    return {
        "max_regressed_metric_count": int_value(
            raw.get("max_regressed_metric_count")
            if "max_regressed_metric_count" in raw
            else 0
        ),
        "min_improved_metric_count": int_value(
            raw.get("min_improved_metric_count")
            if "min_improved_metric_count" in raw
            else 0
        ),
        "max_flaky_metric_count": int_value(
            raw.get("max_flaky_metric_count")
            if "max_flaky_metric_count" in raw
            else 0
        ),
        "accepted_candidate_statuses": statuses or ["ok", "passed"],
    }


def normalize_candidate_evaluation(
    value: Any,
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return candidate_evaluation_error(
            task=task,
            proposal=proposal,
            source=source,
            error_type="TypeError",
            message="candidate evaluation backend returned a non-object",
        )
    evaluation = dict(value)
    evaluation.setdefault("schema_version", 1)
    evaluation.setdefault("kind", CANDIDATE_EVALUATION_KIND)
    evaluation.setdefault("created_at", utc_timestamp())
    evaluation.setdefault("target", task.get("target"))
    evaluation.setdefault("run_id", task.get("run_id"))
    evaluation.setdefault("proposal_id", proposal.get("proposal_id"))
    evaluation.setdefault("source", source)
    errors = list_value(evaluation.get("schema_errors"))
    if evaluation.get("kind") != CANDIDATE_EVALUATION_KIND:
        errors.append(
            {
                "path": "kind",
                "message": f"expected {CANDIDATE_EVALUATION_KIND!r}",
            }
        )
    if evaluation.get("schema_version") != 1:
        errors.append({"path": "schema_version", "message": "expected 1"})
    if evaluation.get("status") not in {"not_run", "passed", "ok", "failed", "error"}:
        errors.append(
            {
                "path": "status",
                "message": "expected one of not_run, passed, ok, failed, error",
            }
        )
    if not isinstance(evaluation.get("baseline_metrics"), dict):
        evaluation["baseline_metrics"] = baseline_metric_snapshot(task)
    if not isinstance(evaluation.get("candidate_metrics"), dict):
        evaluation["candidate_metrics"] = {}
    if errors:
        evaluation["status"] = "error"
        evaluation["schema_errors"] = errors
    return evaluation


def candidate_evaluation_error(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": CANDIDATE_EVALUATION_KIND,
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "source": source,
        "status": "error",
        "baseline_metrics": baseline_metric_snapshot(task),
        "candidate_metrics": {},
        "error": {"type": error_type, "message": message},
    }


def baseline_metric_snapshot(task: dict[str, Any]) -> dict[str, float | int]:
    summary = mapping(task.get("summary"))
    campaign = mapping(summary.get("campaign"))
    harness = mapping(summary.get("harness"))
    campaign_rollup = mapping(summary.get("campaign_rollup"))
    trace_quality = mapping(task.get("trace_quality"))
    metrics: dict[str, float | int] = {}
    metric_sources = (
        harness,
        campaign,
        campaign_rollup,
        trace_quality,
        {"llm_sample_count": summary.get("llm_sample_count")},
    )
    for source in metric_sources:
        for key in (
            "record_count",
            "failed_record_count",
            "hanging_span_count",
            "orphan_span_count",
            "uncovered_line_count",
            "covered_line_count",
            "coverage_percent",
            "case_count",
            "directive_count",
            "round_count",
            "llm_sample_count",
        ):
            value = number_value(source.get(key))
            if value is not None:
                metrics[key] = value
    return metrics


def numeric_metrics(values: dict[str, Any]) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for key, value in values.items():
        number = number_value(value)
        if number is not None:
            metrics[str(key)] = number
    return metrics


def metric_direction(name: str, delta: float | int) -> str:
    if delta == 0:
        return "unchanged"
    if name in LOWER_IS_BETTER_METRICS:
        return "improved" if delta < 0 else "regressed"
    if name in HIGHER_IS_BETTER_METRICS:
        return "improved" if delta > 0 else "regressed"
    return "changed"


def metric_gates_acceptance(name: str) -> bool:
    return name in LOWER_IS_BETTER_METRICS or name in HIGHER_IS_BETTER_METRICS


def metric_role(name: str) -> str:
    if metric_gates_acceptance(name):
        return "quality_gate"
    if name in INFORMATIONAL_METRICS:
        return "informational"
    return "informational"


def summary_metric_count(
    summary: dict[str, Any],
    preferred_key: str,
    fallback_key: str,
) -> int:
    if preferred_key in summary:
        return int_value(summary.get(preferred_key))
    return int_value(summary.get(fallback_key))


def number_value(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def int_value(value: Any) -> int:
    number = number_value(value)
    if number is None:
        return 0
    return int(number)


def candidate_id_for(task: dict[str, Any], proposal: dict[str, Any]) -> str:
    proposal_id = str(proposal.get("proposal_id") or "proposal")
    run_id = str(task.get("run_id") or "run")
    return f"{safe_slug(run_id)}_{safe_slug(proposal_id)}"


def safe_slug(value: str) -> str:
    chars = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in {"-", "_", "."}):
            chars.append(char)
        else:
            chars.append("_")
    slug = "".join(chars).strip("._")
    return slug or "item"


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
