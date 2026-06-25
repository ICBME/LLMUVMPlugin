"""Ref model planning from SemanticSpecIR.

This module builds a backend-neutral ref model plan.  It does not emit Python
code and does not decide SVA lowering.  The plan is the contract consumed by a
future ref model code generator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from Spec2Backend.BackendReadiness import analyze_backend_readiness


REF_MODEL_PLAN_SCHEMA_VERSION = 1

REF_MODEL_BACKEND = "ref_model"

READY = "ready"
PARTIAL = "partial"
EMPTY = "empty"
BLOCKED = "blocked"
BLOCKED_BY_READINESS = "blocked_by_readiness"


class RefModelPlanError(ValueError):
    """Raised when a ready AST cannot be converted into a RefModelPlan rule."""


def build_ref_model_plan(
    semantic_ir: dict[str, Any],
    *,
    readiness: dict[str, Any] | None = None,
    require_readiness_ready: bool = False,
) -> dict[str, Any]:
    """Build a structured ref model lowering plan from SemanticSpecIR."""

    readiness = readiness or analyze_backend_readiness(semantic_ir)
    if require_readiness_ready and readiness.get("status") not in {READY, PARTIAL}:
        return empty_plan(
            semantic_ir,
            readiness=readiness,
            status=BLOCKED_BY_READINESS,
            blocked_items=[
                {
                    "reason": "backend readiness status does not allow ref model planning",
                    "readiness_status": str(readiness.get("status") or "unknown"),
                }
            ],
        )

    readiness_by_element = readiness_by_semantic_element_id(readiness)
    rules: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    not_applicable: list[dict[str, Any]] = []

    for index, element in enumerate(semantic_elements_for_plan(semantic_ir)):
        element_id = str(element.get("id") or f"semantic_elements[{index}]")
        element_readiness = readiness_by_element.get(element_id)
        if element_readiness is None:
            blocked_items.append(
                blocked_item(
                    element,
                    index=index,
                    reason="backend readiness is missing this semantic element",
                    readiness={},
                )
            )
            continue
        if ref_model_ready(element_readiness):
            try:
                rules.extend(lower_semantic_element(element, index=index))
            except RefModelPlanError as exc:
                blocked_items.append(
                    blocked_item(
                        element,
                        index=index,
                        reason=str(exc),
                        readiness=element_readiness,
                    )
                )
            continue
        if has_ref_model_target(element_readiness):
            blocked_items.append(
                blocked_item(
                    element,
                    index=index,
                    reason=ref_model_block_reason(element_readiness),
                    readiness=element_readiness,
                )
            )
            continue
        not_applicable.append(
            {
                "semantic_element_id": element_id,
                "claim_ids": string_list(element.get("claim_ids", [])),
                "reason": "semantic element is not assigned to the ref_model backend",
                "recommended_backends": string_list(element_readiness.get("recommended_backends", [])),
            }
        )

    summary = {
        "semantic_element_count": len(semantic_elements_for_plan(semantic_ir)),
        "rule_count": len(rules),
        "blocked_item_count": len(blocked_items),
        "not_applicable_count": len(not_applicable),
    }
    return {
        "schema_version": REF_MODEL_PLAN_SCHEMA_VERSION,
        "target": str(semantic_ir.get("target") or ""),
        "status": plan_status(summary),
        "readiness": {
            "schema_version": readiness.get("schema_version"),
            "status": readiness.get("status"),
            "review_gate": readiness.get("review_gate", {}),
        },
        "summary": summary,
        "rules": rules,
        "blocked_items": blocked_items,
        "not_applicable": not_applicable,
    }


def empty_plan(
    semantic_ir: dict[str, Any],
    *,
    readiness: dict[str, Any],
    status: str,
    blocked_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": REF_MODEL_PLAN_SCHEMA_VERSION,
        "target": str(semantic_ir.get("target") or ""),
        "status": status,
        "readiness": {
            "schema_version": readiness.get("schema_version"),
            "status": readiness.get("status"),
            "review_gate": readiness.get("review_gate", {}),
        },
        "summary": {
            "semantic_element_count": len(semantic_elements_for_plan(semantic_ir)),
            "rule_count": 0,
            "blocked_item_count": len(blocked_items),
            "not_applicable_count": 0,
        },
        "rules": [],
        "blocked_items": blocked_items,
        "not_applicable": [],
    }


def lower_semantic_element(element: dict[str, Any], *, index: int) -> list[dict[str, Any]]:
    representation = element.get("representation")
    if not isinstance(representation, dict):
        raise RefModelPlanError("semantic element has no RepresentationAST")
    ast = representation.get("ast")
    if not isinstance(ast, dict):
        raise RefModelPlanError("representation.ast must be an object")

    representation_kind = str(representation.get("kind") or "")
    rule = lower_ast_rule(
        ast,
        representation_kind=representation_kind,
    )
    rule.update(
        {
            "id": f"ref_rule_{index + 1}",
            "semantic_element_id": str(element.get("id") or f"semantic_elements[{index}]"),
            "claim_ids": string_list(element.get("claim_ids", [])),
            "evidence": string_list(element.get("evidence", [])),
            "source_ast_node": str(ast.get("node") or ""),
            "source_ast_hash": stable_hash(ast),
            "lowering_kind": f"{representation_kind}:{ast.get('node') or ''}",
        }
    )
    rule["lowered_rule_hash"] = stable_hash({key: value for key, value in rule.items() if key != "lowered_rule_hash"})
    return [rule]


def lower_ast_rule(ast: dict[str, Any], *, representation_kind: str) -> dict[str, Any]:
    node = str(ast.get("node") or "")
    if representation_kind == "combinational_relation":
        return lower_combinational_rule(ast, node=node)
    if representation_kind == "reset_rule":
        return {
            "kind": "reset_rule",
            "condition": lower_optional_expr(ast.get("condition")),
            "effects": lower_expr_list(ast.get("effects", [])),
            "state": lower_optional_expr(ast.get("state")),
            "context": lower_optional_expr(ast.get("context")),
        }
    if representation_kind == "sequential_update":
        return {
            "kind": "sequential_update",
            "event": lower_optional_expr(ast.get("event")),
            "updates": lower_expr_list(ast.get("updates", [])),
            "context": lower_optional_expr(ast.get("context")),
        }
    if representation_kind == "state_machine":
        if node == "fsm":
            return {
                "kind": "state_machine",
                "state_signal": lower_optional_expr(ast.get("state_signal")),
                "states": string_list(ast.get("states", [])),
                "initial_state": ast.get("initial_state"),
                "reset": lower_optional_expr(ast.get("reset")),
                "transitions": lower_expr_list(ast.get("transitions", [])),
                "outputs": lower_expr_list(ast.get("outputs", [])),
                "context": lower_optional_expr(ast.get("context")),
            }
        if node == "state_transition":
            return {"kind": "state_transition", **lower_state_transition(ast)}
    raise RefModelPlanError(f"representation kind {representation_kind!r} is not supported by RefModelPlan")


def lower_combinational_rule(ast: dict[str, Any], *, node: str) -> dict[str, Any]:
    if node == "assignment":
        return {
            "kind": "assignment",
            "target": lower_ref(ast.get("target")),
            "value": lower_expr(ast.get("value")),
            "blocking": bool(ast.get("blocking", False)),
        }
    if node == "constant_relation":
        return {
            "kind": "constant_relation",
            "target": lower_ref(ast.get("target")),
            "value": lower_expr(ast.get("value")),
        }
    if node == "conditional_assignment":
        return {
            "kind": "conditional_assignment",
            "condition": lower_expr(ast.get("condition")),
            "target": lower_ref(ast.get("target")),
            "value": lower_expr(ast.get("value")),
        }
    if node == "operation_relation":
        rule: dict[str, Any] = {
            "kind": "operation_relation",
            "operation": str(ast.get("operation") or ""),
            "operands": lower_expr_list(ast.get("operands", [])),
        }
        if "result" in ast:
            rule["result"] = lower_optional_expr(ast.get("result"))
        if "text" in ast and isinstance(ast.get("text"), str):
            rule["text"] = ast["text"]
        return rule
    raise RefModelPlanError(f"combinational AST node {node!r} is not supported by RefModelPlan")


def lower_expr(expr: Any) -> dict[str, Any]:
    if not isinstance(expr, dict):
        raise RefModelPlanError(f"expected AST expression object, got {type(expr).__name__}")
    node = expr.get("node")
    if node == "field_ref":
        return {"kind": "field", "name": required_string(expr, "name")}
    if node == "signal_ref":
        return {"kind": "signal", "name": required_string(expr, "name")}
    if node == "state_ref":
        return {"kind": "state", "name": required_string(expr, "name")}
    if node == "literal":
        result = {"kind": "literal", "value": expr.get("value")}
        copy_optional(expr, result, "width")
        copy_optional(expr, result, "base")
        return result
    if node == "assignment":
        return {
            "kind": "assignment_expr",
            "target": lower_ref(expr.get("target")),
            "value": lower_expr(expr.get("value")),
            "blocking": bool(expr.get("blocking", False)),
        }
    if node == "compare":
        return {
            "kind": "compare",
            "op": required_string(expr, "op"),
            "left": lower_expr(expr.get("left")),
            "right": lower_expr(expr.get("right")),
        }
    if node == "unary_op":
        return {
            "kind": "unary_op",
            "op": required_string(expr, "op"),
            "operand": lower_expr(expr.get("operand")),
        }
    if node == "binary_op":
        return {
            "kind": "binary_op",
            "op": required_string(expr, "op"),
            "left": lower_expr(expr.get("left")),
            "right": lower_expr(expr.get("right")),
        }
    if node == "reduce":
        return {
            "kind": "reduce",
            "op": required_string(expr, "op"),
            "operand": lower_expr(expr.get("operand")),
        }
    if node == "mux":
        return {
            "kind": "mux",
            "condition": lower_expr(expr.get("condition")),
            "when_true": lower_expr(expr.get("when_true")),
            "when_false": lower_expr(expr.get("when_false")),
        }
    if node == "concat":
        return {"kind": "concat", "parts": lower_expr_list(expr.get("parts", []))}
    if node == "slice":
        return {
            "kind": "slice",
            "value": lower_expr(expr.get("value")),
            "msb": expr.get("msb"),
            "lsb": expr.get("lsb"),
        }
    if node == "cast":
        result = {
            "kind": "cast",
            "type": required_string(expr, "type"),
            "value": lower_expr(expr.get("value")),
        }
        copy_optional(expr, result, "width")
        copy_optional(expr, result, "signed")
        return result
    if node == "clock_event":
        return {
            "kind": "clock_event",
            "edge": str(expr.get("edge") or "any"),
            "signal": lower_ref(expr.get("signal")),
        }
    if node == "clock_reset_context":
        result = {"kind": "clock_reset_context"}
        if "clock" in expr:
            result["clock"] = lower_expr(expr.get("clock"))
        if "reset" in expr:
            result["reset"] = lower_ref(expr.get("reset"))
        copy_optional(expr, result, "reset_polarity")
        copy_optional(expr, result, "reset_synchrony")
        return result
    if node == "state_transition":
        return {"kind": "state_transition", **lower_state_transition(expr)}
    if node == "fsm":
        return {
            "kind": "fsm",
            "states": string_list(expr.get("states", [])),
            "transitions": lower_expr_list(expr.get("transitions", [])),
        }
    raise RefModelPlanError(f"AST node {node!r} is not supported by RefModelPlan")


def lower_state_transition(expr: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": lower_expr(expr.get("from")),
        "to": lower_expr(expr.get("to")),
        "condition": lower_optional_expr(expr.get("condition")),
        "outputs": lower_expr_list(expr.get("outputs", [])),
    }


def lower_ref(expr: Any) -> dict[str, Any]:
    lowered = lower_expr(expr)
    if lowered.get("kind") not in {"field", "signal", "state"}:
        raise RefModelPlanError(f"expected ref node, got {lowered.get('kind')!r}")
    return lowered


def lower_optional_expr(expr: Any) -> dict[str, Any] | None:
    if expr is None:
        return None
    return lower_expr(expr)


def lower_expr_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RefModelPlanError(f"expected expression list, got {type(value).__name__}")
    return [lower_expr(item) for item in value]


def readiness_by_semantic_element_id(readiness: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    elements = readiness.get("elements", [])
    if not isinstance(elements, list):
        return result
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_id = element.get("semantic_element_id")
        if isinstance(element_id, str):
            result[element_id] = element
    return result


def ref_model_ready(element_readiness: dict[str, Any]) -> bool:
    return (
        element_readiness.get("support_status") == READY
        and REF_MODEL_BACKEND in string_list(element_readiness.get("recommended_backends", []))
    )


def has_ref_model_target(element_readiness: dict[str, Any]) -> bool:
    targets = element_readiness.get("lowering_targets", [])
    if not isinstance(targets, list):
        return False
    return any(
        isinstance(target, dict) and target.get("backend") == REF_MODEL_BACKEND
        for target in targets
    )


def ref_model_block_reason(element_readiness: dict[str, Any]) -> str:
    blockers = element_readiness.get("blockers", [])
    if isinstance(blockers, list) and blockers:
        return "semantic element has blockers before ref model planning"
    targets = element_readiness.get("lowering_targets", [])
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict) and target.get("backend") == REF_MODEL_BACKEND:
                return str(target.get("reason") or "ref model target is not ready")
    return "semantic element is not ready for ref model planning"


def blocked_item(
    element: dict[str, Any],
    *,
    index: int,
    reason: str,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    return {
        "semantic_element_id": str(element.get("id") or f"semantic_elements[{index}]"),
        "claim_ids": string_list(element.get("claim_ids", [])),
        "reason": reason,
        "support_status": str(readiness.get("support_status") or "unknown"),
        "blockers": readiness.get("blockers", []),
        "lowering_targets": readiness.get("lowering_targets", []),
    }


def plan_status(summary: dict[str, int]) -> str:
    if summary["blocked_item_count"] and summary["rule_count"]:
        return PARTIAL
    if summary["blocked_item_count"]:
        return BLOCKED
    if summary["rule_count"]:
        return READY
    return EMPTY


def semantic_elements_for_plan(semantic_ir: dict[str, Any]) -> list[dict[str, Any]]:
    semantic_elements = semantic_ir.get("semantic_elements")
    if not isinstance(semantic_elements, list):
        return []
    return [
        element
        for element in semantic_elements
        if isinstance(element, dict)
    ]


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RefModelPlanError(f"{key} must be a non-empty string")
    return item


def copy_optional(source: dict[str, Any], target: dict[str, Any], key: str) -> None:
    if key in source:
        target[key] = source[key]


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def write_ref_model_plan(path: str | Path, plan: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_ref_model_plan(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
