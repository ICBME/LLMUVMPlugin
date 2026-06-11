from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import request

from connector_observe.trace import read_json_object

from .harness_records import mapping


TASK_KIND = "libafl_bfm_fuzz.harness_optimization_task"
PROPOSAL_KIND = "libafl_bfm_fuzz.harness_optimization_proposal"
DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_decision"
PATCH_KIND = "libafl_bfm_fuzz.harness_optimization_patch"
CANDIDATE_MANIFEST_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_manifest"
CANDIDATE_EVALUATION_KIND = "libafl_bfm_fuzz.harness_optimization_candidate_evaluation"
METRIC_DELTA_KIND = "libafl_bfm_fuzz.harness_optimization_metric_delta"
FINAL_DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_final_decision"

ALLOWED_ACTION_TYPES = (
    "mutation_directive_update",
    "stimulus_generation_hint",
    "replay_probe",
    "scoreboard_check",
    "ref_model_patch",
    "coverage_feedback_tuning",
    "documentation_note",
    "no_op",
)

SAFE_SANDBOX_ACTION_TYPES = (
    "mutation_directive_update",
    "stimulus_generation_hint",
    "replay_probe",
    "scoreboard_check",
    "coverage_feedback_tuning",
    "documentation_note",
    "no_op",
)

LOWER_IS_BETTER_METRICS = {
    "failed_record_count",
    "hanging_span_count",
    "orphan_span_count",
    "uncovered_line_count",
}

HIGHER_IS_BETTER_METRICS = {
    "covered_line_count",
    "coverage_percent",
    "case_count",
    "directive_count",
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
}


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


@dataclass(frozen=True)
class HarnessOptimizationPaths:
    task: Path
    proposal: Path
    decision: Path
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


def harness_optimization_paths(evaluation_path: Path) -> HarnessOptimizationPaths:
    stem = evaluation_path.stem
    sandbox_dir = evaluation_path.with_name(f"{stem}_harness_optimization_sandbox")
    return HarnessOptimizationPaths(
        task=evaluation_path.with_name(f"{stem}_harness_optimization_task.json"),
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


@dataclass(frozen=True)
class HarnessOptimizerContext:
    paths: HarnessOptimizationPaths
    cwd: Path


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
        return cls(
            model=model,
            transport=OpenAICompatibleChatTransport.from_env(),
            max_repair_attempts=repairs,
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
        )
        write_json(self.paths.decision, decision)
        return decision

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
) -> dict[str, Any]:
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
            "allowed_action_types": list(ALLOWED_ACTION_TYPES),
            "safe_sandbox_action_types": list(SAFE_SANDBOX_ACTION_TYPES),
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
        "safe_action_dsl": safe_action_dsl_schema(),
        "evidence_ref_fields": ["span_id", "case_id", "directive_id", "connector"],
    }


def safe_action_dsl_schema() -> dict[str, Any]:
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
    }


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
    attempt_index: int,
) -> dict[str, Any]:
    value = llm_response_json(response)
    if isinstance(value, dict) and isinstance(value.get("proposal"), dict):
        value = value["proposal"]
    if not isinstance(value, dict):
        return invalid_optimizer_proposal(
            task,
            source=source,
            reason="LLM response did not contain a JSON object proposal",
            attempt_index=attempt_index,
        )
    proposal = dict(value)
    proposal.setdefault("schema_version", 1)
    proposal.setdefault("kind", PROPOSAL_KIND)
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
    reason: str,
    attempt_index: int | None = None,
) -> dict[str, Any]:
    suffix = "invalid" if attempt_index is None else f"invalid:{attempt_index}"
    return {
        "schema_version": 1,
        "kind": PROPOSAL_KIND,
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


def build_harness_optimization_decision(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    validation = validate_harness_optimization_proposal(proposal, task=task)
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


def build_harness_optimization_patch(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    decision: dict[str, Any],
    paths: HarnessOptimizationPaths,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
            if action_type not in SAFE_SANDBOX_ACTION_TYPES:
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
            "safe_action_types": list(SAFE_SANDBOX_ACTION_TYPES),
            "unsafe_action_types": sorted(
                set(ALLOWED_ACTION_TYPES) - set(SAFE_SANDBOX_ACTION_TYPES)
            ),
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
        if base_value is None or candidate_value is None:
            comparisons.append(
                {
                    "metric": name,
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
                "baseline": base_value,
                "candidate": candidate_value,
                "delta": delta,
                "direction": metric_direction(name, delta),
            }
        )
    improved = sum(1 for item in comparisons if item["direction"] == "improved")
    regressed = sum(1 for item in comparisons if item["direction"] == "regressed")
    unchanged = sum(1 for item in comparisons if item["direction"] == "unchanged")
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
    regressed_metric_count = int_value(delta_summary.get("regressed_metric_count"))
    improved_metric_count = int_value(delta_summary.get("improved_metric_count"))
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
            "acceptance_thresholds": thresholds,
        },
    }


def validate_harness_optimization_proposal(
    proposal: dict[str, Any],
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
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
        allowed = set(ALLOWED_ACTION_TYPES)
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
        if action_type in DSL_PAYLOAD_ACTION_TYPES and not isinstance(payload, dict):
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
) -> list[dict[str, str]]:
    if action_type == "replay_probe":
        return replay_probe_payload_errors(payload, path=path)
    if action_type == "scoreboard_check":
        return scoreboard_check_payload_errors(payload, path=path)
    if action_type == "coverage_feedback_tuning":
        return coverage_feedback_tuning_payload_errors(payload, path=path)
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


def is_string_or_string_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


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


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
