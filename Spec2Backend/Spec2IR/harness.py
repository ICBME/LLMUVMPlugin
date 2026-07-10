"""Harness adapter for LLM-agent driven Spec2IR repair."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any, Iterable

from LLMPlugin import (
    LLMAgentConfig,
    LLMAgentRunner,
    LLMAgentRuntime,
    default_agent_runtime,
)

from .automation import (
    AutomationPolicyGraph,
    classify_review_for_automation,
    deterministic_repair_semantic_spec_ir,
)
from .context import design_ir_context, manifest_context, spec_contexts
from .patch import (
    SemanticIRPatchError,
    apply_semantic_ir_patch,
    normalize_semantic_ir_patch,
    semantic_ir_patch_contract,
    semantic_ir_sha256,
)
from .semantic_ir import (
    augment_semantic_context_from_elements,
    generate_semantic_spec_ir,
    normalize_semantic_spec_ir,
    semantic_spec_ir_contract,
)
from .semantic_validation import validate_semantic_spec_ir
from .validation_review import review_semantic_spec_ir


AGENT_INSTRUCTIONS = (
    "You are a Spec2IR repair agent. Return one strict JSON object. "
    "Treat review_report and repair_focus as authoritative. Resolve every blocking finding while "
    "preserving all source-derived claims, evidence, identifiers, and already valid typed AST. "
    "After a patch is rejected or rolled back, use the finding delta to change strategy; do not "
    "submit the same semantic structure again. "
    "Repair the artifact with action='apply_semantic_ir_patch'. Return only a small "
    "revisioned patch over the LLM-editable semantic plane; never regenerate the complete IR. "
    "Use stable collection item ids and the patch contract from the observation. "
    "Use type='tool_call' only when a patch needs inspection before applying it. "
    "If the source remains ambiguous, patch an explicit open question or semantic gap; "
    "the harness will route it to human review."
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
    best: dict[str, Any] | None = field(default=None, init=False)
    review: dict[str, Any] = field(default_factory=dict, init=False)
    best_review: dict[str, Any] = field(default_factory=dict, init=False)
    status: str = field(default="pending", init=False)
    started: bool = field(default=False, init=False)
    done: bool = field(default=False, init=False)
    automation_decisions: list[dict[str, Any]] = field(default_factory=list, init=False)
    deterministic_repairs: list[dict[str, Any]] = field(default_factory=list, init=False)
    harness_attempts: list[dict[str, Any]] = field(default_factory=list, init=False)
    review_history: list[dict[str, Any]] = field(default_factory=list, init=False)
    patch_history: list[dict[str, Any]] = field(default_factory=list, init=False)
    candidate_hashes: set[str] = field(default_factory=set, init=False)
    revision: int = field(default=0, init=False)
    observation_count: int = field(default=0, init=False)
    last_candidate_review: dict[str, Any] | None = field(default=None, init=False)
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
        self.best = json_round_trip(self.current or {})
        self.best_review = json_round_trip(self.review)
        self.candidate_hashes.add(semantic_ir_sha256(self.current or {}))
        self._update_status_after_review(initial=True)
        return self.observe()

    def observe(self) -> dict[str, Any]:
        if self.current is None:
            return {"status": self.status, "agent_instructions": AGENT_INSTRUCTIONS}
        first_snapshot = self.observation_count == 0
        self.observation_count += 1
        artifact_sha256 = semantic_ir_sha256(self.current)
        observation = {
            "schema_version": 1,
            "workflow": "spec2ir_agent_harness",
            "status": self.status,
            "target": self.target or self.current.get("target"),
            "agent_instructions": AGENT_INSTRUCTIONS,
            "artifact": {
                "id": self.artifact_id(),
                "revision": self.revision,
                "sha256": artifact_sha256,
            },
            "review_report": self.last_candidate_review or self.review,
            "repair_focus": self._repair_focus(),
            "automation_decision": self.automation_decisions[-1] if self.automation_decisions else {},
            "last_error": self.last_error or {},
            "response_contract": {
                "action": "apply_semantic_ir_patch",
                "patch": "required SemanticIRPatch when action is apply_semantic_ir_patch",
                "rationale": "optional concise rationale",
            },
            "allowed_actions": [
                {
                    "action": "apply_semantic_ir_patch",
                    "description": "Atomically apply a small revisioned patch to editable semantic items.",
                },
            ],
            "semantic_ir_patch_contract": {
                **semantic_ir_patch_contract(),
                "base_revision": self.revision,
                "base_sha256": artifact_sha256,
            },
            "tool_specs": self.tool_specs(),
        }
        if first_snapshot:
            observation["context_mode"] = "snapshot"
            observation["current_semantic_spec_ir"] = self.current
            observation["semantic_spec_ir_contract"] = semantic_spec_ir_contract()
            inputs = {
                "target_manifest": manifest_context(self.manifest_path),
                "design_ir": design_ir_context(self.design_ir_path),
                "specs": spec_contexts(self.spec_paths),
            }
            observation["inputs"] = inputs
            observation["session_context"] = {
                "artifact_id": self.artifact_id(),
                "target": self.target or self.current.get("target"),
                "initial_editable_semantic_fragments": editable_semantic_fragments(self.current),
                "inputs": inputs,
                "patch_contract": semantic_ir_patch_contract(),
            }
        else:
            observation["context_mode"] = "delta"
            observation["editable_semantic_fragments"] = editable_semantic_fragments(self.current)
            observation["context_references"] = self.context_references()
            observation["last_transition"] = self.harness_attempts[-1] if self.harness_attempts else {}
        return observation

    def artifact_id(self) -> str:
        target = str(self.target or (self.current or {}).get("target") or "semantic_ir")
        source_identity = "|".join(
            f"{path}:{hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'missing'}"
            for path in self.spec_paths
        )
        digest = hashlib.sha256(f"{target}|{source_identity}".encode("utf-8")).hexdigest()[:16]
        return f"{target}:{digest}"

    def context_references(self) -> dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path) if self.manifest_path is not None else None,
            "design_ir_path": str(self.design_ir_path) if self.design_ir_path is not None else None,
            "spec_paths": [str(path) for path in self.spec_paths],
            "source_hashes": {
                str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.spec_paths
                if path.exists()
            },
        }

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_review_findings",
                "description": "Return the current review report and automation decision.",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_semantic_ir_fragment",
                "description": "Return one editable SemanticSpecIR collection item by stable id.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "collection": {"type": "string"},
                        "id": {"type": "string"},
                    },
                    "required": ["collection", "id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "validate_semantic_ir_patch",
                "description": "Apply and review a revisioned patch transactionally without committing it.",
                "input_schema": {
                    "type": "object",
                    "properties": {"patch": {"type": "object"}},
                    "required": ["patch"],
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "get_review_findings":
            return {
                "status": "tool_result",
                "review": self.review,
                "automation_decision": self.automation_decisions[-1] if self.automation_decisions else {},
                "last_error": self.last_error,
            }
        if name == "get_semantic_ir_fragment":
            collection_name = str(arguments.get("collection") or "")
            item_id = str(arguments.get("id") or "")
            collection = (self.current or {}).get(collection_name)
            if not isinstance(collection, list):
                raise ValueError(f"unknown collection {collection_name!r}")
            item = next(
                (
                    value
                    for value in collection
                    if isinstance(value, dict) and value.get("id") == item_id
                ),
                None,
            )
            if item is None:
                raise ValueError(f"unknown item {item_id!r} in {collection_name}")
            return {
                "status": "tool_result",
                "artifact_revision": self.revision,
                "fragment": item,
            }
        if name == "validate_semantic_ir_patch":
            candidate, review, decision, deterministic, patch_result = self._evaluate_patch(
                arguments.get("patch") if isinstance(arguments.get("patch"), dict) else arguments
            )
            progress = review_progress(self.review, review)
            result = {
                "status": "tool_result",
                "schema_valid": True,
                "valid": True,
                "review_status": review.get("status"),
                "progress": compact_review_progress(progress),
                "candidate_sha256": semantic_ir_sha256(candidate),
                "automation_route": decision.get("route"),
                "deterministic_repair_changed": deterministic is not None,
                "patch_changed": patch_result.get("changed", False),
            }
            if review.get("status") != "passed":
                result["repair_focus"] = build_repair_focus(review, progress=progress)
            return result
        return {"status": "tool_error", "error": {"type": "UnknownTool", "message": f"unsupported tool {name!r}"}}

    def apply(self, action: dict[str, Any]) -> dict[str, Any]:
        if not self.started:
            self.start()
        attempt_index = len(self.harness_attempts) + 1
        if action.get("action") == "apply_semantic_ir_patch":
            return self._apply_patch(action, attempt_index=attempt_index)
        self.last_error = {
            "type": "InvalidAction",
            "message": "Spec2IRHarness only accepts apply_semantic_ir_patch",
        }
        result = {
            "status": "llm_invalid_response",
            "action": action.get("action"),
            "error": self.last_error,
        }
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
            "review_history": list(self.review_history),
            "patch_history": list(self.patch_history),
            "artifact": {
                "id": self.artifact_id(),
                "revision": self.revision,
                "sha256": semantic_ir_sha256(self.current or {}),
            },
            "attempt_count": len(self.harness_attempts),
            "last_error": self.last_error,
        }

    def _apply_patch(self, action: dict[str, Any], *, attempt_index: int) -> dict[str, Any]:
        patch_payload = action.get("patch") if isinstance(action.get("patch"), dict) else action
        normalized_patch: dict[str, Any] | None = None
        try:
            normalized_patch = normalize_semantic_ir_patch(patch_payload)
            candidate, candidate_review, decision, deterministic, patch_result = self._evaluate_patch(
                normalized_patch
            )
        except (SemanticIRPatchError, TypeError, ValueError) as exc:
            self.last_error = {"type": type(exc).__name__, "message": str(exc)}
            self.patch_history.append(
                {
                    "attempt": attempt_index,
                    "accepted": False,
                    "classification": "invalid",
                    "result_revision": self.revision,
                    **({"patch": normalized_patch} if normalized_patch is not None else {}),
                    "error": self.last_error,
                }
            )
            result = {
                "attempt": attempt_index,
                "status": "patch_rejected",
                "action": "apply_semantic_ir_patch",
                "error": self.last_error,
            }
            self.harness_attempts.append(result)
            return result

        candidate_sha256 = semantic_ir_sha256(candidate)
        duplicate_candidate = candidate_sha256 in self.candidate_hashes
        self.candidate_hashes.add(candidate_sha256)
        progress = review_progress(self.review, candidate_review)
        classification = classify_review_progress(self.review, candidate_review, progress)
        progress["classification"] = classification
        progress["candidate_sha256"] = candidate_sha256
        self.last_review_progress = progress
        self.last_candidate_review = candidate_review
        accepted = classification in {"improved", "passed"}
        candidate_snapshot = review_snapshot(candidate_review)
        candidate_snapshot.update(
            {
                "candidate_sha256": candidate_sha256,
                "accepted": accepted,
                "classification": classification,
            }
        )
        self.review_history.append(candidate_snapshot)

        if accepted:
            self.current = candidate
            self.best = json_round_trip(candidate)
            self.review = candidate_review
            self.best_review = json_round_trip(candidate_review)
            self.revision += 1
            if deterministic:
                self.deterministic_repairs.append(deterministic)
            self.automation_decisions.append(decision)
            self._update_status_after_review(initial=False)
            self.last_error = None
        else:
            self.current = json_round_trip(self.best or self.current or {})
            self.review = json_round_trip(self.best_review or self.review)
            self.status = "running"
            self.done = False
            error_type = "CandidateRegressed" if classification in {"regressed", "mixed"} else "NoProgress"
            self.last_error = {
                "type": error_type,
                "message": (
                    "patch was not committed because validation did not improve the best revision"
                ),
                "classification": classification,
            }
            if duplicate_candidate:
                self.status = "stalled"
                self.done = True
                self.last_error = {
                    "type": "RepairStalled",
                    "message": "patch reproduced an already evaluated artifact revision",
                    "classification": classification,
                }

        patch_record = {
            **patch_result,
            "attempt": attempt_index,
            "accepted": accepted,
            "classification": classification,
            "result_revision": self.revision,
        }
        self.patch_history.append(patch_record)
        result = {
            "attempt": attempt_index,
            "status": self.status if self.done else classification,
            "action": "apply_semantic_ir_patch",
            "accepted": accepted,
            "artifact_revision": self.revision,
            "artifact_sha256": semantic_ir_sha256(self.current or {}),
            "candidate_review_status": candidate_review.get("status"),
            "review_feedback": build_repair_focus(candidate_review, progress=progress),
            "progress": progress,
            "automation_decision": decision,
            "error": self.last_error,
        }
        self.harness_attempts.append(result)
        return result

    def _evaluate_patch(
        self,
        patch_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        patch_application = apply_semantic_ir_patch(
            self.current or {},
            patch_payload,
            revision=self.revision,
        )
        if not patch_application.changed:
            raise SemanticIRPatchError("patch does not change the SemanticSpecIR")
        candidate = normalize_semantic_spec_ir(patch_application.semantic_ir)
        semantic_context = candidate.get("semantic_context")
        semantic_elements = candidate.get("semantic_elements")
        if isinstance(semantic_context, dict) and isinstance(semantic_elements, list):
            augment_semantic_context_from_elements(semantic_context, semantic_elements)
        deterministic = deterministic_repair_semantic_spec_ir(candidate, spec_paths=self.spec_paths)
        deterministic_record = None
        if deterministic.changed:
            candidate = deterministic.semantic_ir
            deterministic_record = deterministic.to_dict()
        validate_semantic_spec_ir(
            candidate,
            manifest_path=self.manifest_path,
            spec_paths=self.spec_paths,
            target=self.target,
        )
        candidate_review = review_semantic_spec_ir(
            candidate,
            manifest_path=self.manifest_path,
            spec_paths=self.spec_paths,
            design_ir_path=self.design_ir_path,
            target=self.target,
            require_reviewed=self.require_reviewed,
        )
        decision = classify_review_for_automation(
            candidate_review,
            candidate,
            policy_graph=self.automation_policy_graph,
        ).to_dict()
        return (
            candidate,
            candidate_review,
            decision,
            deterministic_record,
            patch_application.to_dict(),
        )

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

    def _repair_focus(self) -> dict[str, Any]:
        return build_repair_focus(
            self.last_candidate_review or self.review,
            progress=self.last_review_progress,
        )


@dataclass
class Spec2IRAgentSession:
    signature: str
    harness: Spec2IRHarness
    runner: LLMAgentRunner
    result: dict[str, Any] | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


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
    runtime: LLMAgentRuntime | None = None,
    thread_id: str | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    runtime = runtime or default_agent_runtime()
    spec_paths = tuple(spec_paths)
    signature = spec2ir_session_signature(
        manifest_path=manifest_path,
        spec_paths=spec_paths,
        design_ir_path=design_ir_path,
        target=target,
        initial_semantic_ir=initial_semantic_ir,
        require_reviewed=require_reviewed,
        automation_policy_graph=automation_policy_graph,
    )
    resolved_thread_id = thread_id or f"spec2ir:{target or 'semantic_ir'}:{signature[:16]}"
    if not resume:
        runtime.discard_session(resolved_thread_id, delete_checkpoint=True)
    existing = runtime.get_session(resolved_thread_id) if resume else None
    if isinstance(existing, Spec2IRAgentSession) and existing.signature == signature:
        with existing.lock:
            existing.runner.backend = llm_backend
            existing.runner.config = replace(
                existing.runner.config,
                model=model,
                max_attempts=max_attempts,
                run_name=run_name,
            )
            if existing.harness.is_done() and existing.result is not None:
                return json_round_trip(existing.result)
            existing.result = existing.runner.run()
            return existing.result
    if existing is not None:
        runtime.discard_session(resolved_thread_id, delete_checkpoint=True)

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
            metadata={"thread_id": resolved_thread_id, "session_signature": signature},
        ),
        runtime=runtime,
    )
    session = Spec2IRAgentSession(signature=signature, harness=harness, runner=runner)
    runtime.set_session(resolved_thread_id, session)
    session.result = runner.run()
    return session.result


def spec2ir_session_signature(
    *,
    manifest_path: str | Path | None,
    spec_paths: Iterable[str | Path],
    design_ir_path: str | Path | None,
    target: str | None,
    initial_semantic_ir: dict[str, Any] | None,
    require_reviewed: bool,
    automation_policy_graph: AutomationPolicyGraph | None,
) -> str:
    paths = [Path(path) for path in spec_paths]
    if manifest_path is not None:
        paths.append(Path(manifest_path))
    if design_ir_path is not None:
        paths.append(Path(design_ir_path))
    payload = {
        "target": target,
        "require_reviewed": require_reviewed,
        "automation_policy_graph": (
            asdict(automation_policy_graph)
            if automation_policy_graph is not None
            else None
        ),
        "files": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
            for path in paths
        },
        "initial_semantic_ir_sha256": (
            semantic_ir_sha256(initial_semantic_ir) if initial_semantic_ir is not None else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, sort_keys=True))


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
        stable_finding_code(str(finding.get("message") or "")),
    )


def stable_finding_code(message: str) -> str:
    lowered = message.lower()
    patterns = (
        ("not covered by typed representationast", "obligation_not_covered"),
        ("is not covered by semantic_elements", "claim_not_covered"),
        ("is only covered by incomplete", "claim_incomplete"),
        ("must be one of", "enum_violation"),
        ("incompatible value type", "assignment_type_mismatch"),
        ("incompatible types", "operand_type_mismatch"),
        ("must be numeric", "numeric_operand_required"),
        ("must be bitvector", "bitvector_operand_required"),
        ("must be a positive integer", "positive_integer_required"),
        ("requires human review", "human_review_required"),
        ("must be repaired from text_expr", "text_expr_requires_typed_ast"),
    )
    for marker, code in patterns:
        if marker in lowered:
            return code
    normalized = re.sub(r"'[^']*'|\b\d+\b", "?", lowered)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


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
    result = {
        "previous_status": previous.get("status") if isinstance(previous, dict) else None,
        "current_status": current.get("status") if isinstance(current, dict) else None,
        "previous_finding_count": len(previous_findings),
        "current_finding_count": len(current_findings),
        "resolved_count": len(resolved_ids),
        "introduced_count": len(introduced_ids),
        "persistent_count": len(persistent_ids),
        "no_progress": bool(current_findings) and set(previous_by_id) == set(current_by_id),
        "resolved_findings": [previous_by_id[key] for key in sorted(resolved_ids)],
        "introduced_findings": [current_by_id[key] for key in sorted(introduced_ids)],
    }
    classification = classify_review_progress(previous, current, result)
    result["classification"] = classification
    result["made_progress"] = classification in {"improved", "passed"}
    return result


def compact_review_progress(progress: dict[str, Any]) -> dict[str, Any]:
    return {
        key: progress.get(key)
        for key in (
            "previous_status",
            "current_status",
            "previous_finding_count",
            "current_finding_count",
            "resolved_count",
            "introduced_count",
            "persistent_count",
            "no_progress",
            "classification",
            "made_progress",
        )
        if key in progress
    }


def classify_review_progress(
    previous: dict[str, Any],
    current: dict[str, Any],
    progress: dict[str, Any],
) -> str:
    if current.get("status") == "passed":
        return "passed"
    previous_metrics = review_metrics(previous)
    current_metrics = review_metrics(current)
    if (
        current_metrics["protected_errors"] > previous_metrics["protected_errors"]
        or current_metrics["covered_claims"] < previous_metrics["covered_claims"]
        or current_metrics["blocking_findings"] > previous_metrics["blocking_findings"]
    ):
        return "regressed"
    if (
        current_metrics["covered_claims"] > previous_metrics["covered_claims"]
        or current_metrics["blocking_findings"] < previous_metrics["blocking_findings"]
        or current_metrics["error_findings"] < previous_metrics["error_findings"]
    ):
        return "improved"
    if progress.get("resolved_count") and progress.get("introduced_count"):
        return "mixed"
    return "equivalent"


def review_metrics(review: dict[str, Any]) -> dict[str, int]:
    findings = [
        item for item in review.get("findings", []) if isinstance(item, dict)
    ] if isinstance(review, dict) else []
    completeness = review.get("completeness") if isinstance(review.get("completeness"), dict) else {}
    return {
        "blocking_findings": sum(1 for item in findings if item.get("blocking")),
        "error_findings": sum(1 for item in findings if item.get("severity") == "error"),
        "protected_errors": sum(
            1
            for item in findings
            if item.get("stage") in {"schema_review", "traceability_review"}
            and item.get("severity") == "error"
        ),
        "covered_claims": len(completeness.get("covered_claims") or []),
    }


def editable_semantic_fragments(semantic_ir: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json_round_trip(value)
        for key, value in semantic_ir.items()
        if key in {"semantic_elements", "open_questions", "semantic_gaps"}
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
