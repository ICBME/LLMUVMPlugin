from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from harness_optimization.io import write_json
from harness_optimization.action_dsl import builtin_action_plugin
from harness_optimization.optimization import (
    HarnessCandidateEvaluationBackend,
    HarnessLlmTransport,
    LlmOptimizationProposalBackend as SharedLlmOptimizationProposalBackend,
    build_optimization_advice_report as _build_optimization_advice_report,
    build_optimization_prompt as _build_optimization_prompt,
    build_optimization_task as _build_optimization_task,
    NoopOptimizationCandidateEvaluationBackend as SharedNoopCandidateEvaluationBackend,
    NoopOptimizationProposalBackend as SharedNoopOptimizationProposalBackend,
    OptimizationRuntimeAdapter as SharedOptimizationRuntimeAdapter,
    optimization_advice_action_summary as _optimization_advice_action_summary,
    PromptOnlyOptimizationProposalBackend as SharedPromptOnlyOptimizationProposalBackend,
    HarnessOptimizationPaths,
    HarnessOptimizerBackend,
    HarnessOptimizerContext,
    OpenAICompatibleChatTransport,
    harness_optimization_paths,
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
    normalize_candidate_evaluation as _normalize_candidate_evaluation,
    optimizer_proposal_schema_hint as _optimizer_proposal_schema_hint,
    safe_action_dsl_schema as _safe_action_dsl_schema,
    safe_slug,
    validate_harness_optimization_proposal as _validate_harness_optimization_proposal,
)

from .plugins import (
    HarnessActionPlugin,
    HarnessPluginRegistry,
    plugin_registry_provenance_json,
    plugin_registry_to_json,
    plugin_registry_validation_json,
)


TASK_KIND = "libafl_bfm_fuzz.harness_optimization_task"
PROPOSAL_KIND = "libafl_bfm_fuzz.harness_optimization_proposal"
DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_decision"
PATCH_KIND = "libafl_bfm_fuzz.harness_optimization_patch"
CANDIDATE_MANIFEST_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_manifest"
CANDIDATE_EVALUATION_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_evaluation"
METRIC_DELTA_KIND = "libafl_bfm_fuzz.harness_optimization_metric_delta"
FINAL_DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_final_decision"
ADVICE_REPORT_KIND = "libafl_bfm_fuzz.harness_optimization_advice_report"

# Compatibility re-exports consumed through fuzz_pipeline.harness_optimization.
_COMPAT_EXPORTS = (
    HarnessOptimizerContext,
    harness_optimization_paths,
)

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
class NoopHarnessOptimizerBackend(SharedNoopOptimizationProposalBackend):
    source: str = "noop"

    def __post_init__(self) -> None:
        SharedNoopOptimizationProposalBackend.__init__(
            self,
            proposal_kind=PROPOSAL_KIND,
            source=self.source,
            rationale=(
                "No harness optimizer backend is configured; this proposal records "
                "a valid no-op placeholder for downstream validation."
            ),
        )


@dataclass(frozen=True)
class LlmHarnessOptimizerBackend(SharedLlmOptimizationProposalBackend):
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

    def __post_init__(self) -> None:
        SharedLlmOptimizationProposalBackend.__init__(
            self,
            model=self.model,
            prompt_builder=build_harness_optimizer_prompt,
            validator=_proposal_validation,
            proposal_kind=PROPOSAL_KIND,
            response_kind="libafl_bfm_fuzz.harness_optimizer_llm_response",
            transport=self.transport,
            source=self.source,
            temperature=self.temperature,
            max_repair_attempts=self.max_repair_attempts,
            sample_limit=self.sample_limit,
        )


@dataclass(frozen=True)
class PromptOnlyHarnessOptimizerBackend(SharedPromptOnlyOptimizationProposalBackend):
    source: str = "prompt_only"
    model: str = "prompt-only"
    sample_limit: int = 5

    @classmethod
    def from_env(cls) -> "PromptOnlyHarnessOptimizerBackend":
        return cls(
            model=os.getenv("HARNESS_OPTIMIZER_LLM_MODEL", "prompt-only"),
            sample_limit=int(os.getenv("HARNESS_OPTIMIZER_SAMPLE_LIMIT", "5")),
        )

    def __post_init__(self) -> None:
        SharedPromptOnlyOptimizationProposalBackend.__init__(
            self,
            model=self.model,
            prompt_builder=build_harness_optimizer_prompt,
            proposal_kind=PROPOSAL_KIND,
            response_kind="libafl_bfm_fuzz.harness_optimizer_llm_response",
            source=self.source,
            sample_limit=self.sample_limit,
            proposal_rationale=(
                "Prompt-only backend generated no optimization actions; use "
                "the prompt artifact with an external LLM or switch to the llm "
                "backend for structured suggestions."
            ),
            response_reason=(
                "Prompt-only harness optimizer backend wrote the optimizer "
                "prompt for external review and intentionally skipped the LLM "
                "transport call."
            ),
        )


@dataclass(frozen=True)
class NoopHarnessCandidateEvaluationBackend(SharedNoopCandidateEvaluationBackend):
    source: str = "noop"

    def __post_init__(self) -> None:
        SharedNoopCandidateEvaluationBackend.__init__(
            self,
            candidate_evaluation_kind=CANDIDATE_EVALUATION_KIND,
            baseline_metric_snapshot=baseline_metric_snapshot,
            rationale=(
                "No candidate evaluation backend is configured; this report keeps "
                "the validation framework explicit without running a regression."
            ),
            source=self.source,
        )


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
        return self._runtime_adapter().run_proposal(task)

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
        return self._runtime_adapter().run_candidate_evaluation(
            task,
            proposal,
            patch,
            candidate_manifest,
        )

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

    def _runtime_adapter(self) -> SharedOptimizationRuntimeAdapter:
        return SharedOptimizationRuntimeAdapter(
            paths=self.paths,
            cwd=self.cwd,
            optimizer_backend=self.optimizer_backend,
            candidate_evaluation_backend=self.candidate_evaluation_backend,
            proposal_kind=PROPOSAL_KIND,
            normalize_candidate_evaluation=_normalize_candidate_evaluation_for_runtime,
            candidate_evaluation_error=_candidate_evaluation_error_for_runtime,
        )


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
    return _build_optimization_task(
        task_kind=TASK_KIND,
        campaign_evaluation=campaign_evaluation,
        campaign_manifest=campaign_manifest,
        campaign_evaluation_path=campaign_evaluation_path,
        campaign_manifest_path=campaign_manifest_path,
        target=target,
        cwd=cwd,
        objective=(
            "Generate a schema-valid harness optimization proposal grounded in "
            "the supplied connector spans, cases, directives, coverage trends, "
            "and failure clusters. The proposal must not assume that changes "
            "are automatically applied."
        ),
        constraints={
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
        evidence_index_builder=evidence_index_for,
    )


def build_harness_optimizer_prompt(
    task: dict[str, Any],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    schema = optimizer_proposal_schema_hint(task)
    return _build_optimization_prompt(
        task=task,
        sample_limit=sample_limit,
        prompt_kind="libafl_bfm_fuzz.harness_optimizer_prompt",
        schema_hint=schema,
        system_message=(
            "You are a hardware verification harness optimizer. Return only JSON. "
            "Generate safe, sandbox-only harness optimization proposals grounded "
            "in the provided evidence. Do not propose source-code mainline edits."
        ),
    )


def _proposal_validation(
    proposal: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    return validate_harness_optimization_proposal(proposal, task=task)


def _normalize_candidate_evaluation_for_runtime(
    value: Any,
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    return normalize_candidate_evaluation(
        value,
        task=task,
        proposal=proposal,
        source=source,
    )


def _candidate_evaluation_error_for_runtime(
    task: dict[str, Any],
    proposal: dict[str, Any],
    source: str,
    error_type: str,
    message: str,
) -> dict[str, Any]:
    return candidate_evaluation_error(
        task=task,
        proposal=proposal,
        source=source,
        error_type=error_type,
        message=message,
    )


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
    return _build_optimization_advice_report(
        kind=ADVICE_REPORT_KIND,
        task=task,
        proposal=proposal,
        decision=decision,
        paths=paths,
        action_summary_builder=advice_action_summary,
    )


def advice_action_summary(
    action: dict[str, Any],
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    return _optimization_advice_action_summary(action, task, proposal)


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
            "replay_probe": builtin_action_plugin(
                "replay_probe",
                adapter_kind="libafl_bfm_fuzz.harness_candidate_replay_probe_config",
                artifact_role="candidate_replay_probe_config",
                make_var="HARNESS_REPLAY_PROBE_CONFIG",
                runtime_action=True,
            ),
            "scoreboard_check": builtin_action_plugin(
                "scoreboard_check",
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
            "coverage_feedback_tuning": builtin_action_plugin(
                "coverage_feedback_tuning",
                adapter_kind=(
                    "libafl_bfm_fuzz."
                    "harness_candidate_coverage_feedback_tuning_config"
                ),
                artifact_role="candidate_coverage_feedback_tuning_config",
                make_var="HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG",
                runtime_action=True,
            ),
            "mmio_readback": builtin_action_plugin(
                "mmio_readback",
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
