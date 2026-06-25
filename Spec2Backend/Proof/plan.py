"""Internal proof-obligation planning for proof backends."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from Spec2Backend.Checks.model import CheckIssue, TypeSpec, issue

from .model import ProofObligation, ProofPlan


VALID_OBLIGATION_KINDS = {
    "expr_equiv",
    "rule_equiv",
    "step_equiv",
    "constraint_sat",
    "invariant_preservation",
    "semantic_plan_equiv",
    "plan_ir_expr_equiv",
    "plan_ir_step_equiv",
    "wrapper_template_check",
}

DEFAULT_PROOF_SCOPE = ("expr",)
SCOPE_ALIASES = {
    "expr": "expr",
    "expression": "expr",
    "expr_equiv": "expr",
    "rule": "rule",
    "rules": "rule",
    "rule_equiv": "rule",
    "step": "step",
    "state": "step",
    "step_equiv": "step",
    "constraint": "constraint",
    "constraints": "constraint",
    "constraint_sat": "constraint",
    "semantic": "semantic",
    "semantics": "semantic",
    "semantic_plan": "semantic",
    "semantic_plan_equiv": "semantic",
    "implementation": "implementation",
    "impl": "implementation",
    "wrapper": "implementation",
    "wrapper_template_check": "implementation",
    "invariant": "invariant",
    "invariants": "invariant",
    "invariant_preservation": "invariant",
}


def build_proof_plan(
    context: Any,
    *,
    proof_scope: Iterable[str] | str | None = None,
    max_subgoals: int = 64,
    semantic_ir: Mapping[str, Any] | None = None,
    ref_model_plan: Mapping[str, Any] | None = None,
    wrapper_source: str | None = None,
    wrapper_path: str | Path | None = None,
) -> tuple[ProofPlan, tuple[CheckIssue, ...]]:
    scope = normalize_proof_scope(proof_scope)
    obligations: list[ProofObligation] = []
    issues: list[CheckIssue] = []
    metadata = getattr(context, "metadata", {}) or {}
    step_rule_ids = set(str(item) for item in metadata.get("step_rule_ids", ()))
    plan = dict(ref_model_plan or metadata.get("ref_model_plan") or {})
    semantic_subject = dict(semantic_ir or metadata.get("semantic_ir") or {})

    if "expr" in scope:
        obligations.extend(_equivalence_obligations(context, kind="expr_equiv", step_rule_ids=step_rule_ids))
    if "semantic" in scope:
        semantic_obligations, semantic_issues = _semantic_plan_obligations(
            context,
            semantic_ir=semantic_subject,
            ref_model_plan=plan,
        )
        obligations.extend(semantic_obligations)
        issues.extend(semantic_issues)
        obligations.extend(
            _plan_ir_obligations(context, expr_kind="plan_ir_expr_equiv", step_kind="plan_ir_step_equiv", step_rule_ids=step_rule_ids)
        )
    if "rule" in scope:
        obligations.extend(
            item
            for item in _equivalence_obligations(context, kind="rule_equiv", step_rule_ids=step_rule_ids)
            if str(item.rule_id or "") not in step_rule_ids
        )
    if "step" in scope:
        obligations.extend(
            item
            for item in _equivalence_obligations(context, kind="step_equiv", step_rule_ids=step_rule_ids)
            if str(item.rule_id or "") in step_rule_ids or item.metadata.get("is_step") is True
        )
    if "constraint" in scope:
        obligations.extend(_constraint_obligations(context))
    if "implementation" in scope:
        wrapper_obligation, wrapper_issues = _wrapper_obligation(wrapper_source=wrapper_source, wrapper_path=wrapper_path)
        if wrapper_obligation is not None:
            obligations.append(wrapper_obligation)
        issues.extend(wrapper_issues)

    obligations = _split_mux_obligations(obligations)

    if "invariant" in scope:
        issues.append(
            issue(
                "proof",
                "$",
                "invariant preservation proof obligations are registered but not implemented in this Lean4 backend version",
                code="invariant_unimplemented",
            )
        )
    unknown_kinds = sorted({item.kind for item in obligations if item.kind not in VALID_OBLIGATION_KINDS})
    if unknown_kinds:
        issues.append(issue("proof", "$", f"proof plan contains unknown obligation kinds: {unknown_kinds}", code="proof_plan_kind"))
    if len(obligations) > max_subgoals:
        issues.append(
            issue(
                "proof",
                "$",
                f"proof plan has {len(obligations)} subgoals, exceeding max_subgoals={max_subgoals}",
                code="proof_subgoal_limit",
            )
        )

    plan = ProofPlan(
        obligations=tuple(obligations),
        scope=scope,
        metadata={
            "scope": list(scope),
            "subgoal_count": len(obligations),
            "max_subgoals": max_subgoals,
            "obligation_kinds": sorted({item.kind for item in obligations}),
        },
    )
    return plan, tuple(issues)


def _split_mux_obligations(obligations: list[ProofObligation]) -> list[ProofObligation]:
    result: list[ProofObligation] = []
    for obligation in obligations:
        work = [obligation]
        while work:
            current = work.pop()
            split = _split_top_mux(current)
            if split:
                work.extend(reversed(split))
            else:
                result.append(current)
    return result


def _split_top_mux(obligation: ProofObligation) -> list[ProofObligation]:
    if obligation.kind not in {
        "expr_equiv",
        "rule_equiv",
        "step_equiv",
        "semantic_plan_equiv",
        "plan_ir_expr_equiv",
        "plan_ir_step_equiv",
    }:
        return []
    left_mux = _mux_parts(obligation.left)
    if left_mux is not None:
        cond, when_true, when_false = left_mux
        return [
            _replace_obligation_side(obligation, side="left", value=when_true, condition=cond, suffix="then"),
            _replace_obligation_side(obligation, side="left", value=when_false, condition={"not": cond}, suffix="else"),
        ]
    right_mux = _mux_parts(obligation.right)
    if right_mux is not None:
        cond, when_true, when_false = right_mux
        return [
            _replace_obligation_side(obligation, side="right", value=when_true, condition=cond, suffix="then"),
            _replace_obligation_side(obligation, side="right", value=when_false, condition={"not": cond}, suffix="else"),
        ]
    return []


def _replace_obligation_side(
    obligation: ProofObligation,
    *,
    side: str,
    value: Any,
    condition: Any,
    suffix: str,
) -> ProofObligation:
    combined_condition = _combine_conditions(obligation.condition, condition)
    kwargs = {
        "obligation_id": f"{obligation.obligation_id}.{suffix}",
        "kind": obligation.kind,
        "path": obligation.path,
        "left": normalize_expr(value) if side == "left" else obligation.left,
        "right": normalize_expr(value) if side == "right" else obligation.right,
        "condition": normalize_expr(combined_condition) if combined_condition is not None else None,
        "expr": obligation.expr,
        "rule_id": obligation.rule_id,
        "trusted_extern_id": obligation.trusted_extern_id,
        "metadata": {**dict(obligation.metadata), "split": "mux", "branch": suffix},
    }
    return ProofObligation(**kwargs)


def _mux_parts(expr: Any) -> tuple[Any, Any, Any] | None:
    if not isinstance(expr, Mapping):
        return None
    node = str(expr.get("node") or expr.get("kind") or "")
    if node not in {"mux", "if"} and "if" not in expr:
        return None
    spec = expr.get("if", expr)
    if not isinstance(spec, Mapping):
        return None
    cond = spec.get("condition", spec.get("cond"))
    when_true = spec.get("when_true", spec.get("then"))
    when_false = spec.get("when_false", spec.get("else"))
    if cond is None or when_true is None or when_false is None:
        return None
    return normalize_expr(cond), normalize_expr(when_true), normalize_expr(when_false)


def _combine_conditions(base: Any | None, extra: Any | None) -> Any | None:
    if base is None:
        return extra
    if extra is None:
        return base
    return {"and": [base, extra]}


def normalize_proof_scope(value: Iterable[str] | str | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_PROOF_SCOPE
    raw_items: Iterable[Any]
    if isinstance(value, str):
        raw_items = (value,)
    else:
        try:
            iter(value)
        except TypeError:
            raw_items = (value,)
        else:
            raw_items = value
    result: list[str] = []
    for item in raw_items:
        normalized = SCOPE_ALIASES.get(str(item).strip().lower())
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result or DEFAULT_PROOF_SCOPE)


def proof_obligation_hash(obligation: ProofObligation) -> str:
    payload = {
        key: value
        for key, value in asdict(obligation).items()
        if key not in {"metadata"}
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _equivalence_obligations(context: Any, *, kind: str, step_rule_ids: set[str]) -> list[ProofObligation]:
    obligations: list[ProofObligation] = []
    for index, equivalence in enumerate(getattr(context, "equivalences", ())):
        rule_id = str(equivalence.rule_id or f"equivalence_{index + 1}")
        is_step = rule_id in step_rule_ids
        obligation_id = f"{kind}:{rule_id}:{index + 1}"
        obligations.append(
            ProofObligation(
                obligation_id=obligation_id,
                kind=kind,
                path=str(equivalence.path or "$"),
                left=normalize_expr(equivalence.left),
                right=normalize_expr(equivalence.right),
                condition=normalize_expr(equivalence.condition) if equivalence.condition is not None else None,
                rule_id=rule_id,
                trusted_extern_id=getattr(equivalence, "trusted_extern_id", None),
                metadata={"source": "context.equivalences", "index": index, "is_step": is_step},
            )
        )
    return obligations


def _plan_ir_obligations(context: Any, *, expr_kind: str, step_kind: str, step_rule_ids: set[str]) -> list[ProofObligation]:
    obligations: list[ProofObligation] = []
    for index, equivalence in enumerate(getattr(context, "equivalences", ())):
        rule_id = str(equivalence.rule_id or f"equivalence_{index + 1}")
        is_step = rule_id in step_rule_ids
        kind = step_kind if is_step else expr_kind
        obligations.append(
            ProofObligation(
                obligation_id=f"{kind}:{rule_id}:{index + 1}",
                kind=kind,
                path=str(equivalence.path or "$"),
                left=normalize_expr(equivalence.left),
                right=normalize_expr(equivalence.right),
                condition=normalize_expr(equivalence.condition) if equivalence.condition is not None else None,
                rule_id=rule_id,
                trusted_extern_id=getattr(equivalence, "trusted_extern_id", None),
                metadata={"source": "context.equivalences", "index": index, "is_step": is_step},
            )
        )
    return obligations


def _semantic_plan_obligations(
    context: Any,
    *,
    semantic_ir: Mapping[str, Any],
    ref_model_plan: Mapping[str, Any],
) -> tuple[list[ProofObligation], list[CheckIssue]]:
    metadata = getattr(context, "metadata", {}) or {}
    elements = _semantic_elements_from(semantic_ir, metadata)
    rules = ref_model_plan.get("rules", ()) if isinstance(ref_model_plan, Mapping) else ()
    if not elements or not isinstance(rules, list | tuple):
        return [], [
            issue(
                "proof",
                "$",
                "semantic proof scope requires SemanticSpecIR semantic_elements and RefModelPlan rules",
                code="semantic_proof_context_missing",
            )
        ]

    elements_by_id = {str(element.get("id") or ""): element for element in elements if isinstance(element, Mapping)}
    obligations: list[ProofObligation] = []
    issues: list[CheckIssue] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            continue
        rule_id = str(rule.get("id") or f"ref_rule_{index + 1}")
        element_id = str(rule.get("semantic_element_id") or "")
        element = elements_by_id.get(element_id)
        if element is None:
            issues.append(
                issue(
                    "proof",
                    f"ref_model_plan.rules[{index}]",
                    f"semantic element {element_id!r} is not available for semantic proof",
                    rule_id=rule_id,
                    code="semantic_element_missing",
                )
            )
            continue
        semantic_rule = _semantic_rule_expr(element)
        plan_rule = _plan_rule_expr(rule)
        if semantic_rule.get("unsupported"):
            issues.append(
                issue(
                    "proof",
                    f"semantic_elements.{element_id}",
                    str(semantic_rule["unsupported"]),
                    rule_id=rule_id,
                    code="unsupported_semantic_obligation",
                )
            )
            continue
        if plan_rule.get("unsupported"):
            issues.append(
                issue(
                    "proof",
                    f"ref_model_plan.rules[{index}]",
                    str(plan_rule["unsupported"]),
                    rule_id=rule_id,
                    code="unsupported_semantic_obligation",
                )
            )
            continue
        semantic_target = semantic_rule.get("target")
        plan_target = plan_rule.get("target")
        if semantic_target and plan_target and semantic_target != plan_target:
            issues.append(
                issue(
                    "proof",
                    f"ref_model_plan.rules[{index}]",
                    f"semantic target {semantic_target!r} does not match plan target {plan_target!r}",
                    rule_id=rule_id,
                    code="semantic_plan_target_mismatch",
                )
            )
            continue
        semantic_condition = semantic_rule.get("condition")
        plan_condition = plan_rule.get("condition")
        if semantic_condition is not None or plan_condition is not None:
            if semantic_condition is None or plan_condition is None:
                issues.append(
                    issue(
                        "proof",
                        f"ref_model_plan.rules[{index}]",
                        "semantic and plan conditions must both be present or both be absent",
                        rule_id=rule_id,
                        code="semantic_plan_condition_mismatch",
                    )
                )
                continue
            obligations.append(
                ProofObligation(
                    obligation_id=f"semantic_plan_equiv:{rule_id}:condition",
                    kind="semantic_plan_equiv",
                    path=f"ref_model_plan.rules[{index}].condition",
                    left=normalize_expr(semantic_condition),
                    right=normalize_expr(plan_condition),
                    rule_id=rule_id,
                    metadata=_semantic_metadata(element, rule, field="condition"),
                )
            )
        obligations.append(
            ProofObligation(
                obligation_id=f"semantic_plan_equiv:{rule_id}:value",
                kind="semantic_plan_equiv",
                path=f"ref_model_plan.rules[{index}].value",
                left=normalize_expr(semantic_rule.get("value")),
                right=normalize_expr(plan_rule.get("value")),
                condition=normalize_expr(semantic_condition) if semantic_condition is not None else None,
                rule_id=rule_id,
                metadata=_semantic_metadata(element, rule, field="value"),
            )
        )
    return obligations, issues


def _semantic_elements_from(semantic_ir: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    elements = semantic_ir.get("semantic_elements") if isinstance(semantic_ir, Mapping) else None
    if not isinstance(elements, list):
        elements = metadata.get("semantic_elements")
    if not isinstance(elements, list | tuple):
        return []
    return [element for element in elements if isinstance(element, Mapping)]


def _semantic_rule_expr(element: Mapping[str, Any]) -> dict[str, Any]:
    representation = element.get("representation")
    if not isinstance(representation, Mapping):
        return {"unsupported": "semantic element has no RepresentationAST"}
    ast = representation.get("ast")
    if not isinstance(ast, Mapping):
        return {"unsupported": "semantic representation ast must be an object"}
    representation_kind = str(representation.get("kind") or "")
    node = str(ast.get("node") or "")
    if representation_kind == "combinational_relation":
        if node in {"assignment", "constant_relation"}:
            return {"target": ref_name_from_expr(ast.get("target")), "value": ast.get("value"), "condition": None}
        if node == "conditional_assignment":
            return {"target": ref_name_from_expr(ast.get("target")), "value": ast.get("value"), "condition": ast.get("condition")}
    if representation_kind == "state_machine" and node == "state_transition":
        target = ref_name_from_expr(ast.get("state") or ast.get("state_signal"))
        condition = ast.get("condition")
        from_value = ast.get("from")
        if target and from_value is not None:
            state_condition: Any = {"eq": [{"state": target}, from_value]}
            condition = {"and": [state_condition, condition]} if condition is not None else state_condition
        return {"target": target, "value": ast.get("to"), "condition": condition}
    return {"unsupported": f"semantic representation {representation_kind!r}/{node!r} is not supported for Lean semantic proof"}


def _plan_rule_expr(rule: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(rule.get("kind") or "")
    if kind in {"assignment", "constant_relation"}:
        return {"target": ref_name_from_expr(rule.get("target")), "value": rule.get("value"), "condition": None}
    if kind == "conditional_assignment":
        return {"target": ref_name_from_expr(rule.get("target")), "value": rule.get("value"), "condition": rule.get("condition")}
    if kind == "state_transition":
        target = ref_name_from_expr(rule.get("state") or rule.get("state_signal"))
        condition = rule.get("condition")
        from_value = rule.get("from")
        if target and from_value is not None:
            state_condition: Any = {"eq": [{"state": target}, from_value]}
            condition = {"and": [state_condition, condition]} if condition is not None else state_condition
        return {"target": target, "value": rule.get("to"), "condition": condition}
    return {"unsupported": f"RefModelPlan rule kind {kind!r} is not supported for Lean semantic proof"}


def ref_name_from_expr(expr: Any) -> str | None:
    if isinstance(expr, str):
        return expr
    if not isinstance(expr, Mapping):
        return None
    if "name" in expr:
        return str(expr.get("name") or "")
    for key in ("field", "signal", "state"):
        if key in expr:
            value = expr.get(key)
            if isinstance(value, Mapping):
                return str(value.get("name") or value.get(key) or "")
            return str(value or "")
    return None


def _semantic_metadata(element: Mapping[str, Any], rule: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    return {
        "source": "semantic_ref_model_plan",
        "field": field,
        "semantic_element_id": str(element.get("id") or ""),
        "source_ast_hash": str(rule.get("source_ast_hash") or ""),
        "lowered_rule_hash": str(rule.get("lowered_rule_hash") or ""),
        "lowering_kind": str(rule.get("lowering_kind") or ""),
    }


def _wrapper_obligation(
    *,
    wrapper_source: str | None,
    wrapper_path: str | Path | None,
) -> tuple[ProofObligation | None, list[CheckIssue]]:
    if wrapper_source is None and wrapper_path is not None:
        try:
            wrapper_source = Path(wrapper_path).read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return None, [issue("proof", str(wrapper_path), f"could not read wrapper source: {exc}", code="wrapper_source_error")]
    if wrapper_source is None:
        return None, [issue("proof", "$", "implementation proof scope requires wrapper_source or wrapper_path", code="wrapper_source_missing")]
    path = str(wrapper_path or "$.wrapper_source")
    return (
        ProofObligation(
            obligation_id="wrapper_template_check:generated_ref_model",
            kind="wrapper_template_check",
            path=path,
            metadata={
                "source": "generated_wrapper",
                "wrapper_source": wrapper_source,
                "wrapper_sha256": hashlib.sha256(wrapper_source.encode("utf-8")).hexdigest(),
                "implementation_boundary": "wrapper_template_only",
                "trusted_runtime": "RefModelInterpreter",
            },
        ),
        [],
    )


def _constraint_obligations(context: Any) -> list[ProofObligation]:
    obligations: list[ProofObligation] = []
    for index, predicate in enumerate(getattr(context, "predicates", ())):
        rule_id = str(predicate.rule_id or f"constraint_{index + 1}")
        obligations.append(
            ProofObligation(
                obligation_id=f"constraint_sat:{rule_id}:{index + 1}",
                kind="constraint_sat",
                path=str(predicate.path or "$"),
                left=normalize_expr(predicate.expr),
                right=True,
                rule_id=rule_id,
                metadata={"source": "context.predicates", "index": index},
            )
        )
    return obligations


def normalize_expr(expr: Any, *, expected: TypeSpec | None = None) -> Any:
    if isinstance(expr, list):
        return [normalize_expr(item) for item in expr]
    if not isinstance(expr, Mapping):
        return expr

    node = str(expr.get("node") or expr.get("kind") or "")
    if node == "literal" or "literal" in expr:
        value = expr.get("value") if node == "literal" else expr.get("literal")
        normalized = dict(expr)
        if expected is not None and expected.kind == "bitvector" and expected.width and isinstance(value, int):
            normalized.setdefault("width", expected.width)
        return normalized

    if "and" in expr:
        parts = _normalize_bool_chain(expr.get("and", ()), key="and")
        if any(part is False for part in parts):
            return False
        parts = [part for part in parts if part is not True]
        if not parts:
            return True
        if len(parts) == 1:
            return parts[0]
        return {"and": parts}

    if "or" in expr:
        parts = _normalize_bool_chain(expr.get("or", ()), key="or")
        if any(part is True for part in parts):
            return True
        parts = [part for part in parts if part is not False]
        if not parts:
            return False
        if len(parts) == 1:
            return parts[0]
        return {"or": parts}

    if node == "unary_op" or "not" in expr:
        operand = normalize_expr(expr.get("operand", expr.get("not")))
        op = str(expr.get("op") or ("not" if "not" in expr else ""))
        if op in {"logical_not", "not", "reduction_not", "!"} and isinstance(operand, bool):
            return not operand
        normalized = dict(expr)
        if "operand" in normalized:
            normalized["operand"] = operand
        elif "not" in normalized:
            normalized["not"] = operand
        return normalized

    if node == "binary_op" or "binary" in expr:
        spec = dict(expr.get("binary", expr))
        spec["left"] = normalize_expr(spec.get("left"))
        spec["right"] = normalize_expr(spec.get("right"))
        folded = _fold_binary(spec)
        if folded is not None:
            return folded
        if "binary" in expr:
            normalized = dict(expr)
            normalized["binary"] = spec
            return normalized
        return spec

    if node in {"mux", "if"} or "if" in expr:
        spec = dict(expr.get("if", expr))
        cond = normalize_expr(spec.get("condition", spec.get("cond")))
        then_expr = normalize_expr(spec.get("when_true", spec.get("then")))
        else_expr = normalize_expr(spec.get("when_false", spec.get("else")))
        if cond is True:
            return then_expr
        if cond is False:
            return else_expr
        spec["condition"] = cond
        spec["when_true"] = then_expr
        spec["when_false"] = else_expr
        return spec if "if" not in expr else {"if": spec}

    if node == "compare" or "eq" in expr or "compare" in expr:
        if "eq" in expr:
            operands = expr.get("eq")
            if isinstance(operands, list | tuple) and len(operands) == 2:
                left = normalize_expr(operands[0])
                right = normalize_expr(operands[1])
                if _same_literal(left, right):
                    return True
                return {"eq": [left, right]}
            return dict(expr)
        spec = dict(expr.get("compare", expr))
        spec["left"] = normalize_expr(spec.get("left"))
        spec["right"] = normalize_expr(spec.get("right"))
        if _same_literal(spec["left"], spec["right"]) and str(spec.get("op") or "eq") in {"eq", "==", "matches"}:
            return True
        if "compare" in expr:
            return {"compare": spec}
        return spec

    if node == "constraint":
        return {"node": "constraint", "expr": normalize_expr(expr.get("expr"))}

    if node == "implication":
        antecedent = normalize_expr(expr.get("antecedent"))
        consequent = normalize_expr(expr.get("consequent"))
        if antecedent is False or consequent is True:
            return True
        if antecedent is True:
            return consequent
        if consequent is False:
            return {"not": antecedent}
        return {"node": "implication", "antecedent": antecedent, "consequent": consequent}

    normalized = dict(expr)
    for key, value in list(normalized.items()):
        if isinstance(value, Mapping) or isinstance(value, list):
            normalized[key] = normalize_expr(value)
    return normalized


def _normalize_bool_chain(value: Any, *, key: str) -> list[Any]:
    items = value if isinstance(value, list | tuple) else ()
    result: list[Any] = []
    for item in items:
        normalized = normalize_expr(item)
        if isinstance(normalized, Mapping) and key in normalized and len(normalized) == 1:
            nested = normalized.get(key)
            if isinstance(nested, list | tuple):
                result.extend(nested)
                continue
        result.append(normalized)
    return result


def _fold_binary(spec: Mapping[str, Any]) -> Any | None:
    left = spec.get("left")
    right = spec.get("right")
    op = str(spec.get("op") or "")
    if isinstance(left, bool) and isinstance(right, bool):
        if op in {"and", "logical_and", "&&"}:
            return left and right
        if op in {"or", "logical_or", "||"}:
            return left or right
        if op in {"xor", "bitwise_xor", "^"}:
            return left ^ right
    if isinstance(left, int) and isinstance(right, int):
        if op in {"add", "+"}:
            return left + right
        if op in {"sub", "-"}:
            return left - right
        if op in {"mul", "*"}:
            return left * right
    return None


def _same_literal(left: Any, right: Any) -> bool:
    if isinstance(left, bool | int | str) and isinstance(right, bool | int | str):
        return left == right
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    left_value = left.get("value") if left.get("node") == "literal" else left.get("literal")
    right_value = right.get("value") if right.get("node") == "literal" else right.get("literal")
    return left_value is not None and left_value == right_value
