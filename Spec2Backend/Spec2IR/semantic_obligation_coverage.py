"""Claim obligation coverage checks for SemanticSpecIR completeness review."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Hashable, Mapping

from .claim_extraction import gap_requires_human_input
from .representation_ast import collect_representation_completeness_issues, contains_ast_node
from .schema import BLOCKING_FORMALIZATION_STATUSES


ClaimKeyLike = Hashable
ObligationMatcher = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def collect_obligation_coverage(
    source_claims: list[dict[str, Any]],
    *,
    semantic_elements: Any,
    open_questions: Any,
    semantic_gaps: Any,
    review: Any,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> dict[str, Any]:
    """Build a claim-obligation coverage matrix from typed RepresentationAST."""

    claims = []
    missing_by_claim: dict[str, list[dict[str, str]]] = {}
    for claim in source_claims:
        claim_id = claim.get("id")
        if not isinstance(claim_id, str):
            continue
        obligations = claim_obligations(claim)
        obligation_results = [
            obligation_coverage_entry(
                claim_id,
                obligation,
                semantic_elements=semantic_elements,
                open_questions=open_questions,
                semantic_gaps=semantic_gaps,
                review=review,
                ir_claim_id_to_key=ir_claim_id_to_key,
                expected_by_key=expected_by_key,
            )
            for obligation in obligations
        ]
        missing = [
            issue
            for entry in obligation_results
            for issue in entry.get("issues", [])
            if entry.get("status") in {"partial", "uncovered"}
        ]
        if missing:
            missing_by_claim[claim_id] = missing
        claims.append(
            {
                "claim_id": claim_id,
                "status": claim_coverage_status(obligation_results),
                "obligations": obligation_results,
            }
        )
    return {
        "summary": coverage_summary(claims),
        "claims": claims,
        "missing_by_claim": missing_by_claim,
    }


def claim_obligations(claim: dict[str, Any]) -> list[dict[str, Any]]:
    decomposition = claim.get("decomposition")
    if not isinstance(decomposition, dict):
        return []
    obligations = decomposition.get("atomic_obligations")
    if not isinstance(obligations, list):
        return []
    return [item for item in obligations if isinstance(item, dict)]


def obligation_coverage_entry(
    expected_claim_id: str,
    obligation: dict[str, Any],
    *,
    semantic_elements: Any,
    open_questions: Any,
    semantic_gaps: Any,
    review: Any,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> dict[str, Any]:
    obligation_id = str(obligation.get("id") or "")
    obligation_kind = str(obligation.get("kind") or "unknown")
    semantic_coverages = matching_semantic_coverages(
        expected_claim_id,
        obligation,
        semantic_elements,
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    if any(item["status"] == "covered" for item in semantic_coverages):
        return {
            "obligation_id": obligation_id,
            "kind": obligation_kind,
            "status": "covered",
            "covering_items": semantic_coverages,
            "issues": [],
        }
    partial_issues = [
        issue
        for item in semantic_coverages
        for issue in item.get("issues", [])
        if item.get("status") == "partial"
    ]
    if partial_issues:
        return {
            "obligation_id": obligation_id,
            "kind": obligation_kind,
            "status": "partial",
            "covering_items": semantic_coverages,
            "issues": add_obligation_context(partial_issues, obligation),
        }

    question_coverages = matching_question_coverages(
        expected_claim_id,
        open_questions,
        review=review,
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    if question_coverages:
        return {
            "obligation_id": obligation_id,
            "kind": obligation_kind,
            "status": "blocked_by_question",
            "covering_items": question_coverages,
            "issues": [],
        }

    gap_coverages = matching_gap_coverages(
        expected_claim_id,
        semantic_gaps,
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    if gap_coverages:
        return {
            "obligation_id": obligation_id,
            "kind": obligation_kind,
            "status": "blocked_by_gap",
            "covering_items": gap_coverages,
            "issues": [],
        }

    issue = {
        "path": f"spec_claims[{expected_claim_id}].decomposition.atomic_obligations[{obligation_id}]",
        "code": "obligation_uncovered",
        "message": f"claim obligation {obligation_id!r} ({obligation_kind}) is not covered by typed RepresentationAST",
    }
    return {
        "obligation_id": obligation_id,
        "kind": obligation_kind,
        "status": "uncovered",
        "covering_items": [],
        "issues": add_obligation_context([issue], obligation),
    }


def matching_semantic_coverages(
    expected_claim_id: str,
    obligation: dict[str, Any],
    semantic_elements: Any,
    *,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> list[dict[str, Any]]:
    if not isinstance(semantic_elements, list):
        return []
    coverages = []
    for index, element in enumerate(semantic_elements):
        if not isinstance(element, dict):
            continue
        if expected_claim_id not in expected_claim_ids_for_item(
            element,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        ):
            continue
        coverage = semantic_element_covers_obligation(element, obligation)
        if coverage["status"] == "none":
            continue
        coverages.append(
            {
                "type": "semantic_element",
                "id": str(element.get("id") or f"semantic_elements[{index}]"),
                **coverage,
            }
        )
    return coverages


def semantic_element_covers_obligation(
    element: dict[str, Any],
    obligation: dict[str, Any],
) -> dict[str, Any]:
    representation = element.get("representation")
    ast = representation.get("ast") if isinstance(representation, dict) else None
    if not isinstance(ast, dict):
        return partial_coverage(
            "representation.ast",
            "ast_missing_for_obligation",
            "semantic element has no typed AST for this obligation",
        )
    status = element.get("formalization_status")
    if status in BLOCKING_FORMALIZATION_STATUSES:
        return partial_coverage(
            "formalization_status",
            "blocking_formalization_status",
            f"formalization_status {status!r} requires human review before this obligation is complete",
        )
    representation_issues = collect_representation_completeness_issues(representation)
    match = ast_covers_obligation(ast, obligation)
    if match["status"] == "none":
        return match
    if representation_issues:
        return {
            "status": "partial",
            "issues": [
                {
                    "path": issue["path"],
                    "code": issue["code"],
                    "message": issue["message"],
                }
                for issue in representation_issues
            ],
        }
    return match


def ast_covers_obligation(ast: dict[str, Any], obligation: dict[str, Any]) -> dict[str, Any]:
    kind = str(obligation.get("kind") or "")
    if kind == "assumption":
        return covered()
    matcher = OBLIGATION_AST_MATCHERS.get(kind, ast_covers_behavior)
    return matcher(ast, obligation)


def ast_covers_interface_port(ast: dict[str, Any], obligation: dict[str, Any]) -> dict[str, Any]:
    attributes = obligation.get("attributes")
    expected_name = attributes.get("name") if isinstance(attributes, dict) else None
    for node in iter_ast_nodes(ast):
        if node.get("node") != "interface_decl":
            continue
        if isinstance(expected_name, str) and node.get("name") != expected_name:
            continue
        return covered()
    return none()


def ast_covers_condition(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    if ast.get("node") == "conditional_assignment":
        return typed_or_partial(ast.get("condition"), "condition_text_fallback", "condition must be typed AST")
    if ast.get("node") == "state_transition" and "condition" in ast:
        return typed_or_partial(ast.get("condition"), "condition_text_fallback", "condition must be typed AST")
    for node in iter_ast_nodes(ast):
        if node.get("node") == "implication":
            return typed_or_partial(node.get("antecedent"), "condition_text_fallback", "antecedent must be typed AST")
        if node.get("node") == "compare":
            return covered()
    return none()


def ast_covers_trigger(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    for node in iter_ast_nodes(ast):
        if node.get("node") == "latency_rule":
            return typed_or_partial(node.get("trigger"), "text_trigger", "latency trigger must be typed AST")
        if node.get("node") in {"clock_event", "fell", "rose"}:
            return covered()
    return none()


def ast_covers_response(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    for node in iter_ast_nodes(ast):
        node_kind = node.get("node")
        if node_kind in {"assignment", "constant_relation", "conditional_assignment"}:
            value = node.get("value")
            if contains_ast_node(value, "text_expr"):
                return partial_coverage("representation.ast", "text_response", "response value must be typed AST")
            return covered()
        if node_kind == "latency_rule":
            return typed_or_partial(node.get("response"), "text_response", "latency response must be typed AST")
        if node_kind == "implication":
            return typed_or_partial(node.get("consequent"), "text_response", "consequent must be typed AST")
    return none()


def ast_covers_timing(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    if any(node.get("node") == "delay_range" for node in iter_ast_nodes(ast)):
        return covered()
    return none()


def ast_covers_clock(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    return covered() if any(node.get("node") == "clock_event" for node in iter_ast_nodes(ast)) else none()


def ast_covers_reset(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    for node in iter_ast_nodes(ast):
        if node.get("node") == "reset_rule":
            return covered()
        if node.get("node") == "clock_reset_context" and isinstance(node.get("reset"), dict):
            return covered()
    return none()


def ast_covers_protocol(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    for node in iter_ast_nodes(ast):
        if node.get("node") == "protocol_rule":
            property_expr = node.get("property")
            if isinstance(property_expr, dict) and property_expr.get("node") == "handshake_rule":
                return covered()
            return typed_or_partial(property_expr, "protocol_text_fallback", "protocol property must be typed AST")
        if node.get("node") == "handshake_rule":
            return covered()
    return none()


def ast_covers_operation(ast: dict[str, Any], obligation: dict[str, Any]) -> dict[str, Any]:
    expected_operation = expected_operation_name(obligation)
    expected_operands = expected_operation_operand_hints(obligation)
    for node in iter_ast_nodes(ast):
        if node.get("node") != "operation_relation":
            continue
        actual_operation = str(node.get("operation") or "")
        if expected_operation and not operation_names_match(expected_operation, actual_operation):
            continue
        operands = node.get("operands")
        if not isinstance(operands, list) or not operands:
            return partial_coverage(
                "representation.ast.operands",
                "operation_operands_missing",
                "operation obligation must identify typed operand references",
            )
        if any(contains_ast_node(operand, "text_expr") for operand in operands):
            return partial_coverage(
                "representation.ast.operands",
                "operation_operand_text_fallback",
                "operation operands must be typed AST references or expressions",
            )
        if expected_operands and not operand_refs_match_hints(operands, expected_operands):
            return partial_coverage(
                "representation.ast.operands",
                "operation_operand_mismatch",
                "operation operands must match source claim operand semantics",
            )
        return covered()
    return none()


def ast_covers_state_transition(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    for node in iter_ast_nodes(ast):
        if node.get("node") == "state_transition":
            return covered()
        if node.get("node") == "fsm" and node.get("transitions"):
            return covered()
    return none()


def ast_covers_truth_table_row(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    if ast.get("node") == "conditional_assignment":
        return covered()
    for node in iter_ast_nodes(ast):
        if node.get("node") == "conditional_assignment":
            return covered()
    return none()


def ast_covers_behavior(ast: dict[str, Any], _obligation: dict[str, Any] | None = None) -> dict[str, Any]:
    if ast.get("node") == "semantic_claim":
        return partial_coverage(
            "representation.ast",
            "semantic_claim_placeholder",
            "semantic_claim is not a machine-checkable formalization",
        )
    if ast.get("node") == "text_expr":
        return partial_coverage(
            "representation.ast",
            "text_only_behavior",
            "behavior must be represented as typed AST",
        )
    return covered()


OBLIGATION_AST_MATCHERS: dict[str, ObligationMatcher] = {
    "behavior": ast_covers_behavior,
    "clock": ast_covers_clock,
    "condition": ast_covers_condition,
    "constraint": ast_covers_behavior,
    "interface_port": ast_covers_interface_port,
    "operation": ast_covers_operation,
    "protocol": ast_covers_protocol,
    "reset": ast_covers_reset,
    "response": ast_covers_response,
    "state_transition": ast_covers_state_transition,
    "timing": ast_covers_timing,
    "trigger": ast_covers_trigger,
    "truth_table_row": ast_covers_truth_table_row,
}


def typed_or_partial(value: Any, code: str, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return partial_coverage("representation.ast", code, message)
    if contains_ast_node(value, "text_expr"):
        return partial_coverage("representation.ast", code, message)
    return covered()


def matching_question_coverages(
    expected_claim_id: str,
    open_questions: Any,
    *,
    review: Any,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> list[dict[str, Any]]:
    if not isinstance(open_questions, list):
        return []
    answered_question_ids = collect_answered_question_ids(review)
    coverages = []
    for index, question in enumerate(open_questions):
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or f"open_questions[{index}]")
        if question_id in answered_question_ids:
            continue
        if question.get("status") in {"resolved", "closed", "accepted"}:
            continue
        if not question.get("blocking"):
            continue
        if expected_claim_id not in expected_claim_ids_for_item(
            question,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        ):
            continue
        coverages.append(
            {
                "type": "open_question",
                "id": question_id,
                "status": "blocked_by_question",
                "issues": [],
            }
        )
    return coverages


def matching_gap_coverages(
    expected_claim_id: str,
    semantic_gaps: Any,
    *,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> list[dict[str, Any]]:
    if not isinstance(semantic_gaps, list):
        return []
    coverages = []
    for index, gap in enumerate(semantic_gaps):
        if not isinstance(gap, dict) or not gap_requires_human_input(gap):
            continue
        if expected_claim_id not in expected_claim_ids_for_item(
            gap,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        ):
            continue
        coverages.append(
            {
                "type": "semantic_gap",
                "id": str(gap.get("id") or f"semantic_gaps[{index}]"),
                "status": "blocked_by_gap",
                "issues": [],
            }
        )
    return coverages


def expected_claim_ids_for_item(
    item: dict[str, Any],
    *,
    ir_claim_id_to_key: Mapping[str, ClaimKeyLike],
    expected_by_key: Mapping[ClaimKeyLike, str],
) -> list[str]:
    expected_ids: list[str] = []
    claim_ids = item.get("claim_ids", [])
    if not isinstance(claim_ids, list):
        return expected_ids
    for claim_id in claim_ids:
        if not isinstance(claim_id, str):
            continue
        key = ir_claim_id_to_key.get(claim_id)
        if key is None:
            continue
        expected_claim_id = expected_by_key.get(key)
        if expected_claim_id is not None and expected_claim_id not in expected_ids:
            expected_ids.append(expected_claim_id)
    return expected_ids


def collect_answered_question_ids(review: Any) -> set[str]:
    if not isinstance(review, dict):
        return set()
    human_answers = review.get("human_answers", [])
    if not isinstance(human_answers, list):
        return set()
    return {
        answer["question_id"]
        for answer in human_answers
        if isinstance(answer, dict) and isinstance(answer.get("question_id"), str)
    }


def claim_coverage_status(obligations: list[dict[str, Any]]) -> str:
    if not obligations:
        return "no_obligations"
    statuses = {str(item.get("status")) for item in obligations}
    if statuses == {"covered"}:
        return "covered"
    if "uncovered" in statuses:
        return "uncovered"
    if "partial" in statuses:
        return "partial"
    if "blocked_by_question" in statuses:
        return "blocked_by_question"
    if "blocked_by_gap" in statuses:
        return "blocked_by_gap"
    return "partial"


def coverage_summary(claims: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "claim_count": len(claims),
        "obligation_count": 0,
        "covered_obligation_count": 0,
        "partial_obligation_count": 0,
        "uncovered_obligation_count": 0,
        "blocked_obligation_count": 0,
    }
    for claim in claims:
        obligations = claim.get("obligations", [])
        if not isinstance(obligations, list):
            continue
        for obligation in obligations:
            if not isinstance(obligation, dict):
                continue
            summary["obligation_count"] += 1
            status = obligation.get("status")
            if status == "covered":
                summary["covered_obligation_count"] += 1
            elif status == "partial":
                summary["partial_obligation_count"] += 1
            elif status == "uncovered":
                summary["uncovered_obligation_count"] += 1
            elif status in {"blocked_by_gap", "blocked_by_question"}:
                summary["blocked_obligation_count"] += 1
    return summary


def add_obligation_context(
    issues: list[dict[str, str]],
    obligation: dict[str, Any],
) -> list[dict[str, str]]:
    result = []
    obligation_id = str(obligation.get("id") or "")
    kind = str(obligation.get("kind") or "unknown")
    for issue in issues:
        result.append(
            {
                "obligation_id": obligation_id,
                "obligation_kind": kind,
                **issue,
            }
        )
    return result


def expected_operation_name(obligation: dict[str, Any]) -> str:
    attributes = obligation.get("attributes")
    if isinstance(attributes, dict) and isinstance(attributes.get("operation"), str):
        return attributes["operation"]
    return ""


def expected_operation_operand_hints(obligation: dict[str, Any]) -> set[str]:
    text = str(obligation.get("text") or "").lower()
    subjects = {
        normalize_name(subject)
        for subject in obligation.get("subjects", [])
        if isinstance(subject, str)
    }
    hints: set[str] = set()
    if "message" in text or "message" in subjects:
        hints.update({"message", "msg"})
    elif "data" in text or "data" in subjects:
        hints.add("data")
    elif "payload" in text or "payload" in subjects:
        hints.add("payload")
    elif "key" in text or "key" in subjects:
        hints.add("key")
    elif "input" in text or "input" in subjects:
        hints.add("input")
    return {normalize_name(hint) for hint in hints if hint}


def operand_refs_match_hints(operands: list[Any], hints: set[str]) -> bool:
    operand_names = {
        normalize_name(name)
        for operand in operands
        for name in iter_ref_names(operand)
    }
    return bool(operand_names.intersection(hints))


def iter_ref_names(expr: Any):
    if isinstance(expr, dict):
        if expr.get("node") in {"field_ref", "signal_ref"} and isinstance(expr.get("name"), str):
            yield expr["name"]
        for value in expr.values():
            yield from iter_ref_names(value)
    elif isinstance(expr, list):
        for item in expr:
            yield from iter_ref_names(item)


def operation_names_match(expected: str, actual: str) -> bool:
    expected = normalize_name(expected)
    actual = normalize_name(actual)
    if not expected:
        return bool(actual)
    if expected == actual:
        return True
    if expected == "sha" and actual.startswith("sha"):
        return True
    if expected == "functionaloperation" and actual:
        return True
    return False


def normalize_name(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def covered() -> dict[str, Any]:
    return {"status": "covered", "issues": []}


def none() -> dict[str, Any]:
    return {"status": "none", "issues": []}


def partial_coverage(path: str, code: str, message: str) -> dict[str, Any]:
    return {
        "status": "partial",
        "issues": [
            {
                "path": path,
                "code": code,
                "message": message,
            }
        ],
    }


def iter_ast_nodes(expr: Any):
    if isinstance(expr, dict):
        if isinstance(expr.get("node"), str):
            yield expr
        for value in expr.values():
            yield from iter_ast_nodes(value)
    elif isinstance(expr, list):
        for item in expr:
            yield from iter_ast_nodes(item)
