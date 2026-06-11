from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Protocol

from connector_observe.trace import read_json_object

from .harness_records import mapping


TASK_KIND = "libafl_bfm_fuzz.harness_optimization_task"
PROPOSAL_KIND = "libafl_bfm_fuzz.harness_optimization_proposal"
DECISION_KIND = "libafl_bfm_fuzz.harness_optimization_decision"

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


class HarnessOptimizerBackend(Protocol):
    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class HarnessOptimizationPaths:
    task: Path
    proposal: Path
    decision: Path

    def to_json(self) -> dict[str, str]:
        return {
            "harness_optimization_task": str(self.task),
            "harness_optimization_proposal": str(self.proposal),
            "harness_optimization_decision": str(self.decision),
        }


def harness_optimization_paths(evaluation_path: Path) -> HarnessOptimizationPaths:
    stem = evaluation_path.stem
    return HarnessOptimizationPaths(
        task=evaluation_path.with_name(f"{stem}_harness_optimization_task.json"),
        proposal=evaluation_path.with_name(
            f"{stem}_harness_optimization_proposal.json"
        ),
        decision=evaluation_path.with_name(
            f"{stem}_harness_optimization_decision.json"
        ),
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
class HarnessOptimizationAdapter:
    target: str
    paths: HarnessOptimizationPaths
    campaign_evaluation_path: Path
    campaign_manifest_path: Path
    cwd: Path
    optimizer_backend: HarnessOptimizerBackend = NoopHarnessOptimizerBackend()

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
    harness_evaluation_path = _resolved_path(trace_artifacts.get("harness_evaluation"), cwd)
    llm_dataset_path = _resolved_path(
        trace_artifacts.get("llm_optimization_dataset"),
        cwd,
    )
    campaign_rollup_path = _resolved_path(
        trace_artifacts.get("campaign_trace_rollup"),
        cwd,
    )
    harness_evaluation = read_json_object(harness_evaluation_path)
    campaign_rollup = read_json_object(campaign_rollup_path)
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
        },
        "summary": {
            "campaign": mapping(campaign_evaluation.get("summary")),
            "harness": mapping(harness_evaluation.get("summary"))
            or mapping(harness_trace.get("summary")),
            "campaign_rollup": mapping(campaign_rollup.get("summary"))
            or mapping(harness_trace.get("campaign_rollup", {})).get("summary", {}),
            "llm_sample_count": count_jsonl_items(llm_dataset_path),
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
        "evidence_index": evidence_index,
        "constraints": {
            "allowed_action_types": list(ALLOWED_ACTION_TYPES),
            "requires_evidence_refs": True,
            "application_mode": "proposal_only",
            "default_validation": "schema_only",
            "sandbox_apply": False,
        },
        "objective": (
            "Generate a schema-valid harness optimization proposal grounded in "
            "the supplied connector spans, cases, directives, coverage trends, "
            "and failure clusters. The proposal must not assume that changes "
            "are automatically applied."
        ),
    }


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


def validate_harness_optimization_proposal(
    proposal: dict[str, Any],
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if proposal.get("kind") != PROPOSAL_KIND:
        errors.append({"path": "kind", "message": f"expected {PROPOSAL_KIND!r}"})
    if proposal.get("schema_version") != 1:
        errors.append({"path": "schema_version", "message": "expected schema_version 1"})
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
            {"path": "actions", "message": "proposed status requires at least one action"}
        )

    allowed = set(list_value(mapping(task.get("constraints")).get("allowed_action_types")))
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


def _optional_path(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}
