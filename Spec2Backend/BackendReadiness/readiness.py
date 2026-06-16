"""Backend readiness analysis for reviewed SemanticSpecIR documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from Spec2Backend.Spec2IR.representation_ast import (
    collect_representation_completeness_issues,
    contains_ast_node,
)
from Spec2Backend.Spec2IR.schema import BLOCKING_FORMALIZATION_STATUSES


BACKEND_READINESS_SCHEMA_VERSION = 1

REF_MODEL_BACKEND = "ref_model"
SVA_BACKEND = "sva"

READY = "ready"
NEEDS_HUMAN_INPUT = "needs_human_input"
UNSUPPORTED = "unsupported"
BLOCKED_BY_REVIEW = "blocked_by_review"

SUPPORTED_REF_MODEL_OPERATIONS = {
    "andgate",
    "notgate",
    "orgate",
    "sha224",
    "sha256",
    "sha384",
    "sha512",
    "xor",
    "xorgate",
}

REF_MODEL_COMBINATIONAL_NODES = {
    "assignment",
    "binary_op",
    "cast",
    "compare",
    "concat",
    "conditional_assignment",
    "constant_relation",
    "field_ref",
    "literal",
    "mux",
    "reduce",
    "signal_ref",
    "slice",
    "unary_op",
}

REF_MODEL_STATEFUL_NODES = REF_MODEL_COMBINATIONAL_NODES | {
    "clock_event",
    "clock_reset_context",
    "fsm",
    "reset_rule",
    "sequential_update",
    "state_ref",
    "state_transition",
}

SVA_NODES = REF_MODEL_COMBINATIONAL_NODES | {
    "clock_event",
    "clock_reset_context",
    "constraint",
    "delay_range",
    "fell",
    "handshake_rule",
    "implication",
    "latency_rule",
    "past",
    "protocol_rule",
    "reset_disable",
    "reset_rule",
    "rose",
    "sequence",
    "signal_binding",
    "stable",
    "temporal_rule",
    "throughout",
    "until",
}


def analyze_backend_readiness(
    semantic_ir: dict[str, Any],
    *,
    review: dict[str, Any] | None = None,
    require_review_passed: bool = False,
) -> dict[str, Any]:
    """Classify SemanticSpecIR elements by backend lowering readiness."""

    review_status = str(review.get("status")) if isinstance(review, dict) else "not_provided"
    review_blocking = require_review_passed and review_status != "passed"
    elements = [
        analyze_semantic_element(
            element,
            index=index,
        )
        for index, element in enumerate(semantic_elements_for_readiness(semantic_ir))
        if isinstance(element, dict)
    ]
    claims = claim_readiness(elements)
    summary = readiness_summary(elements)
    status = overall_status(summary, review_blocking=review_blocking)
    return {
        "schema_version": BACKEND_READINESS_SCHEMA_VERSION,
        "target": str(semantic_ir.get("target") or ""),
        "status": status,
        "review_gate": {
            "required": require_review_passed,
            "status": review_status,
            "passed": review_status == "passed",
            "blocking": review_blocking,
        },
        "summary": summary,
        "elements": elements,
        "claims": claims,
    }


def analyze_semantic_element(element: dict[str, Any], *, index: int) -> dict[str, Any]:
    element_id = str(element.get("id") or f"semantic_elements[{index}]")
    representation = element.get("representation")
    ast = representation.get("ast") if isinstance(representation, dict) else None
    representation_kind = representation.get("kind") if isinstance(representation, dict) else None
    formalization_status = str(element.get("formalization_status") or "")
    blockers = element_blockers(element, representation)
    if not isinstance(ast, dict):
        targets: list[dict[str, Any]] = []
        capability_status = NEEDS_HUMAN_INPUT
        ast_node = ""
    else:
        targets = classify_lowering_targets(
            ast,
            representation_kind=str(representation_kind or ""),
        )
        capability_status = element_capability_status(targets, blockers)
        ast_node = str(ast.get("node") or "")
    recommended_backends = sorted(
        {
            target["backend"]
            for target in targets
            if target.get("status") == READY and not blockers
        }
    )
    return {
        "semantic_element_id": element_id,
        "claim_ids": string_list(element.get("claim_ids", [])),
        "kind": str(element.get("kind") or ""),
        "formalization_status": formalization_status,
        "representation_kind": str(representation_kind or ""),
        "ast_node": ast_node,
        "support_status": capability_status,
        "recommended_backends": recommended_backends,
        "lowering_targets": targets,
        "blockers": blockers,
        "traceability": {
            "evidence": string_list(element.get("evidence", [])),
            "claim_ids": string_list(element.get("claim_ids", [])),
        },
    }


def element_blockers(
    element: dict[str, Any],
    representation: Any,
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    status = element.get("formalization_status")
    if status in BLOCKING_FORMALIZATION_STATUSES:
        blockers.append(
            {
                "code": "blocking_formalization_status",
                "path": "formalization_status",
                "message": f"formalization_status {status!r} requires human review",
            }
        )
    if not isinstance(representation, dict):
        blockers.append(
            {
                "code": "representation_missing",
                "path": "representation",
                "message": "semantic element has no RepresentationAST",
            }
        )
        return blockers
    for issue in collect_representation_completeness_issues(representation):
        blockers.append(
            {
                "code": issue["code"],
                "path": issue["path"],
                "message": issue["message"],
            }
        )
    return blockers


def classify_lowering_targets(
    ast: dict[str, Any],
    *,
    representation_kind: str,
) -> list[dict[str, Any]]:
    node = str(ast.get("node") or "")
    if node in {"semantic_claim", "text_expr"} or contains_ast_node(ast, "text_expr"):
        return [
            target_result(
                backend=REF_MODEL_BACKEND,
                status=NEEDS_HUMAN_INPUT,
                reason="placeholder_or_textual_ast_requires_human_formalization",
            ),
            target_result(
                backend=SVA_BACKEND,
                status=NEEDS_HUMAN_INPUT,
                reason="placeholder_or_textual_ast_requires_human_formalization",
            ),
        ]
    if representation_kind == "combinational_relation":
        return [classify_ref_model_combinational(ast)]
    if representation_kind in {"sequential_update", "state_machine"}:
        return [classify_ref_model_stateful(ast)]
    if representation_kind == "reset_rule":
        return [
            classify_ref_model_stateful(ast),
            classify_sva_rule(ast, require_clock=False),
        ]
    if representation_kind == "temporal_rule":
        return [classify_sva_rule(ast, require_clock=True)]
    if representation_kind == "protocol_rule":
        return [classify_sva_rule(ast, require_clock=True)]
    if representation_kind == "constraint":
        return [classify_sva_rule(ast, require_clock=False)]
    if representation_kind == "interface_decl":
        return [
            target_result(
                backend="metadata",
                status=READY,
                reason="interface declarations are metadata for backend binding",
            )
        ]
    if representation_kind == "example_trace":
        return [
            target_result(
                backend="test_vector",
                status=UNSUPPORTED,
                reason="example traces require a dedicated test-vector backend",
            )
        ]
    return [
        target_result(
            backend=REF_MODEL_BACKEND,
            status=UNSUPPORTED,
            reason=f"representation kind {representation_kind!r} has no readiness rule",
        )
    ]


def classify_ref_model_combinational(ast: dict[str, Any]) -> dict[str, Any]:
    node = str(ast.get("node") or "")
    if node == "operation_relation":
        return classify_ref_model_operation(ast)
    unsupported_nodes = unsupported_ast_nodes(ast, REF_MODEL_COMBINATIONAL_NODES)
    if unsupported_nodes:
        return target_result(
            backend=REF_MODEL_BACKEND,
            status=UNSUPPORTED,
            reason="combinational AST contains nodes without ref model lowering support",
            unsupported_nodes=unsupported_nodes,
        )
    return target_result(
        backend=REF_MODEL_BACKEND,
        status=READY,
        reason="typed combinational relation can be lowered to a pure ref model expression",
    )


def classify_ref_model_operation(ast: dict[str, Any]) -> dict[str, Any]:
    operation = normalize_name(str(ast.get("operation") or ""))
    operands = ast.get("operands")
    if operation not in SUPPORTED_REF_MODEL_OPERATIONS:
        return target_result(
            backend=REF_MODEL_BACKEND,
            status=UNSUPPORTED,
            reason=f"operation {operation!r} has no ref model lowering rule",
        )
    if not isinstance(operands, list) or not operands:
        return target_result(
            backend=REF_MODEL_BACKEND,
            status=NEEDS_HUMAN_INPUT,
            reason="operation lowering requires typed operands",
        )
    unsupported_nodes = unsupported_ast_nodes(ast, REF_MODEL_COMBINATIONAL_NODES | {"operation_relation"})
    if unsupported_nodes:
        return target_result(
            backend=REF_MODEL_BACKEND,
            status=UNSUPPORTED,
            reason="operation operands contain unsupported ref model nodes",
            unsupported_nodes=unsupported_nodes,
        )
    return target_result(
        backend=REF_MODEL_BACKEND,
        status=READY,
        reason=f"operation {operation!r} has a ref model lowering rule",
    )


def classify_ref_model_stateful(ast: dict[str, Any]) -> dict[str, Any]:
    unsupported_nodes = unsupported_ast_nodes(ast, REF_MODEL_STATEFUL_NODES)
    if unsupported_nodes:
        return target_result(
            backend=REF_MODEL_BACKEND,
            status=UNSUPPORTED,
            reason="stateful AST contains nodes without ref model lowering support",
            unsupported_nodes=unsupported_nodes,
        )
    return target_result(
        backend=REF_MODEL_BACKEND,
        status=READY,
        reason="typed state/update relation can be lowered to a step-based ref model",
    )


def classify_sva_rule(ast: dict[str, Any], *, require_clock: bool) -> dict[str, Any]:
    unsupported_nodes = unsupported_ast_nodes(ast, SVA_NODES)
    if unsupported_nodes:
        return target_result(
            backend=SVA_BACKEND,
            status=UNSUPPORTED,
            reason="AST contains nodes without SVA lowering support",
            unsupported_nodes=unsupported_nodes,
        )
    if require_clock and not has_clock_event(ast):
        return target_result(
            backend=SVA_BACKEND,
            status=NEEDS_HUMAN_INPUT,
            reason="SVA lowering requires an explicit clock context",
            requirements=["clock_reset_context.clock or temporal_rule.clock"],
        )
    return target_result(
        backend=SVA_BACKEND,
        status=READY,
        reason="typed temporal/protocol/constraint AST can be lowered to SVA",
    )


def element_capability_status(
    targets: list[dict[str, Any]],
    blockers: list[dict[str, str]],
) -> str:
    if blockers:
        return NEEDS_HUMAN_INPUT
    statuses = {str(target.get("status")) for target in targets}
    if READY in statuses:
        return READY
    if NEEDS_HUMAN_INPUT in statuses:
        return NEEDS_HUMAN_INPUT
    return UNSUPPORTED


def target_result(
    *,
    backend: str,
    status: str,
    reason: str,
    requirements: list[str] | None = None,
    unsupported_nodes: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "backend": backend,
        "status": status,
        "reason": reason,
    }
    if requirements:
        result["requirements"] = requirements
    if unsupported_nodes:
        result["unsupported_ast_nodes"] = unsupported_nodes
    return result


def readiness_summary(elements: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "semantic_element_count": len(elements),
        "ready_element_count": 0,
        "needs_human_input_count": 0,
        "unsupported_element_count": 0,
        "ref_model_ready_count": 0,
        "sva_ready_count": 0,
    }
    for element in elements:
        status = element.get("support_status")
        if status == READY:
            summary["ready_element_count"] += 1
        elif status == NEEDS_HUMAN_INPUT:
            summary["needs_human_input_count"] += 1
        elif status == UNSUPPORTED:
            summary["unsupported_element_count"] += 1
        backends = set(element.get("recommended_backends", []))
        if REF_MODEL_BACKEND in backends:
            summary["ref_model_ready_count"] += 1
        if SVA_BACKEND in backends:
            summary["sva_ready_count"] += 1
    return summary


def overall_status(summary: dict[str, int], *, review_blocking: bool) -> str:
    if review_blocking:
        return BLOCKED_BY_REVIEW
    if summary.get("needs_human_input_count", 0):
        return NEEDS_HUMAN_INPUT
    ready_count = summary.get("ready_element_count", 0)
    unsupported_count = summary.get("unsupported_element_count", 0)
    if ready_count and unsupported_count:
        return "partial"
    if ready_count:
        return READY
    return UNSUPPORTED


def claim_readiness(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_claim: dict[str, list[dict[str, Any]]] = {}
    for element in elements:
        for claim_id in element.get("claim_ids", []):
            if isinstance(claim_id, str):
                by_claim.setdefault(claim_id, []).append(element)
    claims = []
    for claim_id in sorted(by_claim):
        claim_elements = by_claim[claim_id]
        statuses = sorted({str(item.get("support_status")) for item in claim_elements})
        backends = sorted(
            {
                backend
                for item in claim_elements
                for backend in item.get("recommended_backends", [])
                if isinstance(backend, str)
            }
        )
        claims.append(
            {
                "claim_id": claim_id,
                "support_status": aggregate_status(statuses),
                "semantic_element_ids": [
                    str(item.get("semantic_element_id"))
                    for item in claim_elements
                ],
                "recommended_backends": backends,
            }
        )
    return claims


def semantic_elements_for_readiness(semantic_ir: dict[str, Any]) -> list[dict[str, Any]]:
    semantic_elements = semantic_ir.get("semantic_elements")
    if not isinstance(semantic_elements, list):
        return []
    return [
        element
        for element in semantic_elements
        if isinstance(element, dict)
    ]


def aggregate_status(statuses: list[str]) -> str:
    if READY in statuses:
        return READY
    if NEEDS_HUMAN_INPUT in statuses:
        return NEEDS_HUMAN_INPUT
    if UNSUPPORTED in statuses:
        return UNSUPPORTED
    return "unknown"


def unsupported_ast_nodes(ast: dict[str, Any], supported_nodes: set[str]) -> list[str]:
    unsupported = {
        node
        for node in ast_node_names(ast)
        if node not in supported_nodes
    }
    return sorted(unsupported)


def ast_node_names(expr: Any):
    if isinstance(expr, dict):
        node = expr.get("node")
        if isinstance(node, str):
            yield node
        for value in expr.values():
            yield from ast_node_names(value)
    elif isinstance(expr, list):
        for item in expr:
            yield from ast_node_names(item)


def has_clock_event(expr: Any) -> bool:
    return any(node == "clock_event" for node in ast_node_names(expr))


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def normalize_name(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def write_backend_readiness(path: str | Path, readiness: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(readiness, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_backend_readiness(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
