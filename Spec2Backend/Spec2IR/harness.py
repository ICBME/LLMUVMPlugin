"""Harness adapter for LLM-agent driven Spec2IR repair."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable

from LLMPlugin import LLMAgentConfig, LLMAgentRunner

from .automation import (
    AutomationPolicyGraph,
    classify_review_for_automation,
    deterministic_repair_semantic_spec_ir,
)
from .semantic_ir import (
    generate_semantic_spec_ir,
    normalize_semantic_spec_ir_response,
    semantic_spec_ir_contract,
    validate_semantic_spec_ir,
)
from .semantic_repair import design_ir_payload, manifest_payload, spec_payloads
from .validation_review import review_semantic_spec_ir


AGENT_INSTRUCTIONS = (
    "You are a Spec2IR repair agent. Return one strict JSON object. "
    "Treat review_report and repair_focus as authoritative. Resolve every blocking finding while "
    "preserving all source-derived claims, evidence, identifiers, and already valid typed AST. "
    "After a review_failed submission, use the finding delta to change the candidate; do not submit "
    "the same semantic structure again. "
    "Use legacy action='submit_semantic_spec_ir' or modern "
    "type='harness_action' with action='submit_semantic_spec_ir' and a complete "
    "semantic_spec_ir when the provided sources support an automated repair. "
    "Use type='tool_call' to inspect or validate a candidate before submitting it. "
    "Use action='mark_human_required' when the review requires external design intent. "
    "Use action='request_finalize' only when the current review is already acceptable."
)


@dataclass
class Spec2IRHarness:
    manifest_path: str | Path | None = None
    spec_paths: Iterable[str | Path] = ()
    design_ir_path: str | Path | None = None
    target: str | None = None
    require_reviewed: bool = False
    automation_policy_graph: AutomationPolicyGraph | None = None
    initial_semantic_ir: dict[str, Any] | None = None

    current: dict[str, Any] | None = field(default=None, init=False)
    review: dict[str, Any] = field(default_factory=dict, init=False)
    status: str = field(default="pending", init=False)
    started: bool = field(default=False, init=False)
    done: bool = field(default=False, init=False)
    automation_decisions: list[dict[str, Any]] = field(default_factory=list, init=False)
    deterministic_repairs: list[dict[str, Any]] = field(default_factory=list, init=False)
    harness_attempts: list[dict[str, Any]] = field(default_factory=list, init=False)
    review_history: list[dict[str, Any]] = field(default_factory=list, init=False)
    last_review_progress: dict[str, Any] | None = field(default=None, init=False)
    last_error: dict[str, Any] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.spec_paths = tuple(Path(path) for path in self.spec_paths)
        self.manifest_path = Path(self.manifest_path) if self.manifest_path is not None else None
        self.design_ir_path = Path(self.design_ir_path) if self.design_ir_path is not None else None

    def start(self) -> dict[str, Any]:
        if self.started:
            return self.observe()
        self.started = True
        if self.initial_semantic_ir is None:
            if self.manifest_path is None:
                raise ValueError("manifest_path is required when initial_semantic_ir is not provided")
            self.current = generate_semantic_spec_ir(
                manifest_path=self.manifest_path,
                spec_paths=self.spec_paths,
                design_ir_path=self.design_ir_path,
                target=self.target,
            )
        else:
            self.current = json_round_trip(self.initial_semantic_ir)
        self._repair_and_review()
        self._update_status_after_review(initial=True)
        return self.observe()

    def observe(self) -> dict[str, Any]:
        if self.current is None:
            return {"status": self.status, "agent_instructions": AGENT_INSTRUCTIONS}
        return {
            "schema_version": 1,
            "workflow": "spec2ir_agent_harness",
            "status": self.status,
            "target": self.target or self.current.get("target"),
            "agent_instructions": AGENT_INSTRUCTIONS,
            "current_semantic_spec_ir": self.current,
            "review_report": self.review,
            "repair_focus": self._repair_focus(),
            "automation_decision": self.automation_decisions[-1] if self.automation_decisions else {},
            "deterministic_repairs": list(self.deterministic_repairs),
            "harness_attempts": list(self.harness_attempts),
            "last_error": self.last_error or {},
            "semantic_spec_ir_contract": semantic_spec_ir_contract(),
            "response_contract": {
                "action": "submit_semantic_spec_ir | mark_human_required | request_finalize",
                "semantic_spec_ir": "required complete SemanticSpecIR when action is submit_semantic_spec_ir",
                "rationale": "optional concise rationale",
                "assumptions": ["optional assumptions"],
            },
            "allowed_actions": [
                {
                    "action": "submit_semantic_spec_ir",
                    "description": "Submit one complete corrected SemanticSpecIR object.",
                },
                {
                    "action": "mark_human_required",
                    "description": "Stop when the remaining issue needs external human design intent.",
                },
                {
                    "action": "request_finalize",
                    "description": "Finalize only if the current review is already passed.",
                },
            ],
            "tool_specs": self.tool_specs(),
            "inputs": {
                "target_manifest": manifest_payload(self.manifest_path) if self.manifest_path is not None else None,
                "design_ir": design_ir_payload(self.design_ir_path) if self.design_ir_path is not None else None,
                "specs": spec_payloads(self.spec_paths),
            },
        }

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_current_semantic_ir",
                "description": "Return the current complete SemanticSpecIR held by the harness.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_review_findings",
                "description": "Return the current review report and automation decision.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "normalize_semantic_ir_candidate",
                "description": "Normalize a candidate SemanticSpecIR payload without applying it.",
                "input_schema": {
                    "type": "object",
                    "properties": {"semantic_spec_ir": {"type": "object"}},
                    "required": ["semantic_spec_ir"],
                    "additionalProperties": True,
                },
            },
            {
                "name": "validate_semantic_ir_candidate",
                "description": "Normalize and schema-validate a candidate SemanticSpecIR without applying it.",
                "input_schema": {
                    "type": "object",
                    "properties": {"semantic_spec_ir": {"type": "object"}},
                    "required": ["semantic_spec_ir"],
                    "additionalProperties": True,
                },
            },
            {
                "name": "diff_semantic_ir",
                "description": "Return a small structural diff between the current IR and a candidate IR.",
                "input_schema": {
                    "type": "object",
                    "properties": {"semantic_spec_ir": {"type": "object"}},
                    "required": ["semantic_spec_ir"],
                    "additionalProperties": True,
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_current_semantic_ir":
            return {"status": "tool_result", "semantic_ir": self.current or {}}
        if name == "get_review_findings":
            return {
                "status": "tool_result",
                "review": self.review,
                "automation_decision": self.automation_decisions[-1] if self.automation_decisions else {},
                "last_error": self.last_error,
            }
        if name == "normalize_semantic_ir_candidate":
            candidate = self._normalize_tool_candidate(arguments)
            return {"status": "tool_result", "normalized_semantic_ir": candidate}
        if name == "validate_semantic_ir_candidate":
            candidate = self._normalize_tool_candidate(arguments)
            validate_semantic_spec_ir(
                candidate,
                manifest_path=self.manifest_path,
                spec_paths=self.spec_paths,
                target=self.target,
            )
            review = review_semantic_spec_ir(
                candidate,
                manifest_path=self.manifest_path,
                spec_paths=self.spec_paths,
                design_ir_path=self.design_ir_path,
                target=self.target,
                require_reviewed=self.require_reviewed,
            )
            return {
                "status": "tool_result",
                "valid": True,
                "review_status": review.get("status"),
                "review": review,
                "repair_focus": build_repair_focus(
                    review,
                    progress=review_progress(self.review, review),
                ),
            }
        if name == "diff_semantic_ir":
            candidate = self._normalize_tool_candidate(arguments)
            return {"status": "tool_result", "diff": semantic_ir_diff(self.current or {}, candidate)}
        return {"status": "tool_error", "error": {"type": "UnknownTool", "message": f"unsupported tool {name!r}"}}

    def apply(self, action: dict[str, Any]) -> dict[str, Any]:
        if not self.started:
            self.start()
        name = str(action.get("action") or "")
        attempt_index = len(self.harness_attempts) + 1
        if name == "submit_semantic_spec_ir":
            return self._apply_candidate(action, attempt_index=attempt_index)
        if name == "mark_human_required":
            self.done = True
            self.status = "needs_human_input"
            result = {
                "status": self.status,
                "action": name,
                "rationale": action.get("rationale"),
            }
            self.harness_attempts.append(result)
            return result
        if name == "request_finalize":
            self._finalize_from_review()
            result = {"status": self.status, "action": name}
            self.harness_attempts.append(result)
            return result
        self.last_error = {"type": "InvalidAction", "message": f"unsupported action {name!r}"}
        result = {"status": "llm_invalid_response", "action": name, "error": self.last_error}
        self.harness_attempts.append(result)
        return result

    def _normalize_tool_candidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        candidate_payload = arguments.get("semantic_spec_ir")
        if candidate_payload is None and isinstance(arguments.get("candidate"), dict):
            candidate_payload = arguments["candidate"]
        return normalize_semantic_spec_ir_response(
            {"semantic_spec_ir": candidate_payload} if isinstance(candidate_payload, dict) else arguments
        )

    def is_done(self) -> bool:
        return self.done

    def result(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "semantic_ir": self.current or {},
            "review": self.review,
            "automation_decisions": list(self.automation_decisions),
            "deterministic_repairs": list(self.deterministic_repairs),
            "harness_attempts": list(self.harness_attempts),
            "review_history": list(self.review_history),
            "attempt_count": len(self.harness_attempts),
            "last_error": self.last_error,
        }

    def _apply_candidate(self, action: dict[str, Any], *, attempt_index: int) -> dict[str, Any]:
        try:
            candidate_payload = action.get("semantic_spec_ir")
            if candidate_payload is None and isinstance(action.get("candidate"), dict):
                candidate_payload = action["candidate"]
            candidate = normalize_semantic_spec_ir_response(
                {"semantic_spec_ir": candidate_payload} if isinstance(candidate_payload, dict) else action
            )
        except (TypeError, ValueError) as exc:
            self.last_error = {"type": type(exc).__name__, "message": str(exc)}
            result = {
                "attempt": attempt_index,
                "status": "llm_invalid_response",
                "action": "submit_semantic_spec_ir",
                "error": self.last_error,
            }
            self.harness_attempts.append(result)
            return result
        previous_review = self.review
        self.current = candidate
        self._repair_and_review()
        self.last_review_progress = review_progress(previous_review, self.review)
        self._update_status_after_review(initial=False)
        if self.done:
            self.last_error = None
        else:
            finding_count = len(self.review.get("findings", [])) if isinstance(self.review.get("findings"), list) else 0
            self.last_error = {
                "type": "ReviewFailed",
                "message": f"candidate still has {finding_count} review finding(s)",
                "finding_count": finding_count,
            }
        result = {
            "attempt": attempt_index,
            "status": self.status if self.done else "review_failed",
            "action": "submit_semantic_spec_ir",
            "review_status": self.review.get("status"),
            "review_feedback": self._repair_focus(),
            "progress": self.last_review_progress,
            "automation_decision": self.automation_decisions[-1] if self.automation_decisions else {},
        }
        self.harness_attempts.append(result)
        return result

    def _repair_and_review(self) -> None:
        if self.current is None:
            return
        deterministic = deterministic_repair_semantic_spec_ir(
            self.current,
            spec_paths=self.spec_paths,
        )
        if deterministic.changed:
            self.current = deterministic.semantic_ir
            self.deterministic_repairs.append(deterministic.to_dict())
        self.review = review_semantic_spec_ir(
            self.current,
            manifest_path=self.manifest_path,
            spec_paths=self.spec_paths,
            design_ir_path=self.design_ir_path,
            target=self.target,
            require_reviewed=self.require_reviewed,
        )
        self.review_history.append(review_snapshot(self.review))
        decision = classify_review_for_automation(
            self.review,
            self.current,
            policy_graph=self.automation_policy_graph,
        )
        self.automation_decisions.append(decision.to_dict())

    def _update_status_after_review(self, *, initial: bool) -> None:
        review_status = self.review.get("status")
        decision = self.automation_decisions[-1] if self.automation_decisions else {}
        if review_status == "passed":
            self.status = "valid" if initial else "repaired"
            self.done = True
            return
        if review_status == "needs_human_input" and decision.get("route") not in {"llm_repair", "llm_formalize"}:
            self.status = "needs_human_input"
            self.done = True
            return
        self.status = "running"
        self.done = False

    def _finalize_from_review(self) -> None:
        if self.review.get("status") == "passed":
            self.status = "valid" if not self.harness_attempts else "repaired"
        elif self.review.get("status") == "needs_human_input":
            self.status = "needs_human_input"
        else:
            self.status = "repair_failed"
        self.done = True

    def _repair_focus(self) -> dict[str, Any]:
        return build_repair_focus(self.review, progress=self.last_review_progress)


def run_spec2ir_agent(
    *,
    llm_backend: Any,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
    require_reviewed: bool = False,
    automation_policy_graph: AutomationPolicyGraph | None = None,
    initial_semantic_ir: dict[str, Any] | None = None,
    model: str | None = None,
    max_attempts: int = 3,
    run_name: str = "spec2ir_agent",
) -> dict[str, Any]:
    harness = Spec2IRHarness(
        manifest_path=manifest_path,
        spec_paths=spec_paths,
        design_ir_path=design_ir_path,
        target=target,
        require_reviewed=require_reviewed,
        automation_policy_graph=automation_policy_graph,
        initial_semantic_ir=initial_semantic_ir,
    )
    runner = LLMAgentRunner(
        harness,
        backend=llm_backend,
        config=LLMAgentConfig(
            model=model,
            max_attempts=max_attempts,
            run_name=run_name,
            tags=("spec2ir", str(target or "")),
        ),
    )
    return runner.run()


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, sort_keys=True))


def semantic_ir_diff(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_keys = set(current)
    candidate_keys = set(candidate)
    current_ids = semantic_element_ids(current)
    candidate_ids = semantic_element_ids(candidate)
    return {
        "top_level_added": sorted(candidate_keys - current_keys),
        "top_level_removed": sorted(current_keys - candidate_keys),
        "top_level_changed": sorted(
            key for key in current_keys & candidate_keys if current.get(key) != candidate.get(key)
        ),
        "semantic_elements": {
            "current_count": len(current.get("semantic_elements", []))
            if isinstance(current.get("semantic_elements"), list)
            else None,
            "candidate_count": len(candidate.get("semantic_elements", []))
            if isinstance(candidate.get("semantic_elements"), list)
            else None,
            "added_ids": sorted(candidate_ids - current_ids),
            "removed_ids": sorted(current_ids - candidate_ids),
        },
    }


def semantic_element_ids(semantic_ir: dict[str, Any]) -> set[str]:
    elements = semantic_ir.get("semantic_elements")
    if not isinstance(elements, list):
        return set()
    return {
        str(item.get("id"))
        for item in elements
        if isinstance(item, dict) and item.get("id") is not None
    }


def review_snapshot(review: dict[str, Any]) -> dict[str, Any]:
    findings = review.get("findings")
    finding_list = [dict(item) for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    completeness = review.get("completeness") if isinstance(review.get("completeness"), dict) else {}
    return {
        "status": review.get("status"),
        "finding_count": len(finding_list),
        "blocking_finding_count": sum(1 for item in finding_list if item.get("blocking")),
        "findings": finding_list,
        "covered_claims": list(completeness.get("covered_claims") or []),
        "partial_claims": list(completeness.get("partial_claims") or []),
        "uncovered_claims": list(completeness.get("uncovered_claims") or []),
    }


def finding_identity(finding: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(finding.get("stage") or ""),
        str(finding.get("path") or ""),
        str(finding.get("message") or ""),
    )


def review_progress(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous_findings = [
        item for item in previous.get("findings", []) if isinstance(item, dict)
    ] if isinstance(previous, dict) else []
    current_findings = [
        item for item in current.get("findings", []) if isinstance(item, dict)
    ] if isinstance(current, dict) else []
    previous_by_id = {finding_identity(item): item for item in previous_findings}
    current_by_id = {finding_identity(item): item for item in current_findings}
    resolved_ids = previous_by_id.keys() - current_by_id.keys()
    introduced_ids = current_by_id.keys() - previous_by_id.keys()
    persistent_ids = current_by_id.keys() & previous_by_id.keys()
    return {
        "previous_status": previous.get("status") if isinstance(previous, dict) else None,
        "current_status": current.get("status") if isinstance(current, dict) else None,
        "previous_finding_count": len(previous_findings),
        "current_finding_count": len(current_findings),
        "resolved_count": len(resolved_ids),
        "introduced_count": len(introduced_ids),
        "persistent_count": len(persistent_ids),
        "made_progress": len(resolved_ids) > len(introduced_ids) or len(current_findings) < len(previous_findings),
        "no_progress": bool(current_findings) and set(previous_by_id) == set(current_by_id),
        "resolved_findings": [previous_by_id[key] for key in sorted(resolved_ids)],
        "introduced_findings": [current_by_id[key] for key in sorted(introduced_ids)],
    }


def build_repair_focus(
    review: dict[str, Any],
    *,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = review.get("findings")
    finding_list = [dict(item) for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    finding_list.sort(key=lambda item: (not bool(item.get("blocking")), str(item.get("path") or "")))
    completeness = review.get("completeness") if isinstance(review.get("completeness"), dict) else {}
    obligations = completeness.get("claim_obligations")
    missing_obligations = []
    if isinstance(obligations, list):
        missing_obligations = [
            {
                "claim_id": item.get("claim_id"),
                "missing_obligations": item.get("missing_obligations"),
            }
            for item in obligations
            if isinstance(item, dict) and item.get("missing_obligations")
        ]
    return {
        "goal": "Resolve every blocking finding without dropping source claims or valid semantics.",
        "review_status": review.get("status"),
        "blocking_findings": [item for item in finding_list if item.get("blocking")],
        "other_findings": [item for item in finding_list if not item.get("blocking")],
        "missing_claim_obligations": missing_obligations,
        "uncovered_claims": list(completeness.get("uncovered_claims") or []),
        "partial_claims": list(completeness.get("partial_claims") or []),
        "progress_from_previous_submission": progress or {},
    }


__all__ = ["Spec2IRHarness", "run_spec2ir_agent"]
