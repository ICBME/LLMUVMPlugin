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
)
from .semantic_repair import design_ir_payload, manifest_payload, spec_payloads
from .validation_review import review_semantic_spec_ir


AGENT_INSTRUCTIONS = (
    "You are a Spec2IR repair agent. Return one strict JSON action object. "
    "Use action='submit_semantic_spec_ir' with a complete semantic_spec_ir when "
    "the provided sources support an automated repair. Use action='mark_human_required' "
    "when the review requires external design intent. Use action='request_finalize' "
    "only when the current review is already acceptable."
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
            "inputs": {
                "target_manifest": manifest_payload(self.manifest_path) if self.manifest_path is not None else None,
                "design_ir": design_ir_payload(self.design_ir_path) if self.design_ir_path is not None else None,
                "specs": spec_payloads(self.spec_paths),
            },
        }

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
        self.current = candidate
        self._repair_and_review()
        self._update_status_after_review(initial=False)
        result = {
            "attempt": attempt_index,
            "status": self.status if self.done else "review_failed",
            "action": "submit_semantic_spec_ir",
            "review_status": self.review.get("status"),
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
    max_attempts: int = 2,
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


__all__ = ["Spec2IRHarness", "run_spec2ir_agent"]
