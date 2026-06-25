"""Checker adapters for SemanticSpecIR and RefModelIR."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .model import (
    AssignmentCheck,
    CheckContext,
    CheckIssue,
    EquivalenceCheck,
    ExpressionCheck,
    ExternCheck,
    PredicateCheck,
    Symbol,
    TotalityCheck,
    issue,
    type_from_spec,
)


BLOCKING_FORMALIZATION_STATUSES = {
    "ambiguous",
    "incomplete",
    "conflict",
    "needs_human_review",
}


class SemanticSpecIRAdapter:
    def __init__(self, *, manifest_fields: set[str] | None = None):
        self.manifest_fields = manifest_fields or set()

    def schema_issues(self, ir: Mapping[str, Any], *, base_dir: Path) -> list[CheckIssue]:
        issues: list[CheckIssue] = []
        context = ir.get("semantic_context")
        if not isinstance(context, Mapping):
            return [issue("schema", "semantic_context", "must be an object")]
        if context.get("version") != 1:
            issues.append(issue("schema", "semantic_context.version", "must be 1"))
        symbols = context.get("symbols")
        if not isinstance(symbols, list):
            issues.append(issue("schema", "semantic_context.symbols", "must be a list"))
            symbols = []
        seen: set[str] = set()
        for index, item in enumerate(symbols):
            path = f"semantic_context.symbols[{index}]"
            if not isinstance(item, Mapping):
                issues.append(issue("schema", path, "symbol must be an object"))
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name:
                issues.append(issue("schema", f"{path}.name", "must be a non-empty string"))
                continue
            if name in seen:
                issues.append(issue("schema", f"{path}.name", f"duplicate symbol {name!r}"))
            seen.add(name)
            if not isinstance(item.get("kind"), str) or not item.get("kind"):
                issues.append(issue("schema", f"{path}.kind", "must be a non-empty string"))
            if not isinstance(item.get("type"), Mapping):
                issues.append(issue("schema", f"{path}.type", "must be an object"))
            if not isinstance(item.get("roles"), list):
                issues.append(issue("schema", f"{path}.roles", "must be a list"))
            if not isinstance(item.get("source"), str) or not item.get("source"):
                issues.append(issue("schema", f"{path}.source", "must be a non-empty string"))
        constraints = context.get("constraints")
        if not isinstance(constraints, list):
            issues.append(issue("schema", "semantic_context.constraints", "must be a list"))
        for name in self.manifest_fields:
            if name not in seen:
                issues.append(issue("schema", "semantic_context.symbols", f"manifest field {name!r} must have a symbol"))
        for index, item in enumerate(ir.get("inputs", []) if isinstance(ir.get("inputs"), list) else []):
            if isinstance(item, Mapping):
                name = str(item.get("name") or "")
                if name and name not in seen:
                    issues.append(issue("reference", f"inputs[{index}].name", f"input {name!r} has no semantic_context symbol"))
        return issues

    def context(self, ir: Mapping[str, Any]) -> CheckContext:
        semantic_context = dict(ir.get("semantic_context", {}))
        symbols = _symbols_from_context(semantic_context.get("symbols", ()))
        expressions: list[ExpressionCheck] = []
        assignments: list[AssignmentCheck] = []
        predicates: list[PredicateCheck] = []

        constraints = semantic_context.get("constraints", ())
        if isinstance(constraints, list):
            for index, constraint in enumerate(constraints):
                expr = constraint.get("expr", constraint.get("ast")) if isinstance(constraint, Mapping) else constraint
                if expr is not None:
                    predicates.append(PredicateCheck(path=f"semantic_context.constraints[{index}]", expr=expr))

        elements = ir.get("semantic_elements", ())
        if isinstance(elements, list):
            for index, element in enumerate(elements):
                if not isinstance(element, Mapping):
                    continue
                if element.get("formalization_status") in BLOCKING_FORMALIZATION_STATUSES:
                    continue
                representation = element.get("representation")
                if not isinstance(representation, Mapping):
                    continue
                ast = representation.get("ast")
                if not isinstance(ast, Mapping):
                    continue
                path = f"semantic_elements[{index}].representation.ast"
                _semantic_ast_checks(ast, path, expressions, assignments, predicates, rule_id=str(element.get("id") or ""))

        return CheckContext(
            target=str(ir.get("target") or ""),
            symbols=symbols,
            expressions=tuple(expressions),
            assignments=tuple(assignments),
            predicates=tuple(predicates),
            metadata={
                "schema_version": ir.get("schema_version"),
                "target": ir.get("target"),
                "subject": "SemanticSpecIR",
                "semantic_ir": dict(ir),
                "semantic_elements": tuple(dict(element) for element in elements if isinstance(element, Mapping)) if isinstance(elements, list) else (),
                "semantic_context": dict(semantic_context),
            },
        )


class RefModelIRAdapter:
    def __init__(self, *, ref_model_plan: Mapping[str, Any] | None = None):
        self.ref_model_plan = ref_model_plan

    def schema_issues(self, ir: Mapping[str, Any], *, base_dir: Path) -> list[CheckIssue]:
        issues: list[CheckIssue] = []
        if ir.get("schema_version") != 1:
            issues.append(issue("schema", "schema_version", "unsupported RefModelIR schema_version"))
        if not str(ir.get("target") or "").strip():
            issues.append(issue("schema", "target", "RefModelIR must define target"))
        inputs = ir.get("inputs")
        outputs = ir.get("outputs")
        state = ir.get("state", {})
        rules = ir.get("rules")
        step_rules = ir.get("step_rules", [])
        extern_map = ir.get("externs", {})
        if not isinstance(inputs, Mapping):
            issues.append(issue("schema", "inputs", "RefModelIR inputs must be a mapping"))
            inputs = {}
        if not isinstance(outputs, Mapping) or not outputs:
            issues.append(issue("schema", "outputs", "RefModelIR outputs must be a non-empty mapping"))
            outputs = {}
        if state is None:
            state = {}
        if not isinstance(state, Mapping):
            issues.append(issue("schema", "state", "RefModelIR state must be a mapping"))
            state = {}
        if extern_map is None:
            extern_map = {}
        if not isinstance(extern_map, Mapping):
            issues.append(issue("schema", "externs", "externs must be a mapping"))
            extern_map = {}
        if not isinstance(rules, list):
            issues.append(issue("schema", "rules", "RefModelIR rules must be a list"))
            rules = []
        if step_rules is None:
            step_rules = []
        if not isinstance(step_rules, list):
            issues.append(issue("schema", "step_rules", "RefModelIR step_rules must be a list"))
            step_rules = []
        if not rules and not step_rules:
            issues.append(issue("schema", "rules", "RefModelIR must define rules or step_rules"))

        fields = set(str(name) for name in inputs) | set(str(name) for name in state)
        outputs_names = set(str(name) for name in outputs)
        externs = set(str(name) for name in extern_map)
        for collection_name, collection in (("inputs", inputs), ("state", state), ("outputs", outputs)):
            for name, spec in collection.items():
                if not isinstance(spec, Mapping):
                    continue
                typ = str(spec.get("type") or "")
                if typ == "enum" and not spec.get("choices"):
                    issues.append(issue("schema", f"{collection_name}.{name}.choices", "enum fields must define choices"))
                if typ == "bitvector":
                    width = spec.get("width")
                    if not isinstance(width, int) or width <= 0:
                        issues.append(issue("schema", f"{collection_name}.{name}.width", "bitvector fields must define a positive width"))
        for index, rule in enumerate(rules):
            path = f"rules[{index}]"
            if not isinstance(rule, Mapping):
                issues.append(issue("schema", path, "rule must be a mapping"))
                continue
            assign = rule.get("assign")
            if not isinstance(assign, Mapping) or not assign:
                issues.append(issue("schema", f"{path}.assign", "rule must assign at least one output"))
                continue
            for name, expr in assign.items():
                if str(name) not in outputs_names:
                    issues.append(issue("schema", f"{path}.assign.{name}", f"unknown output {name!r}"))
                issues.extend(_expr_reference_issues(expr, fields=fields, externs=externs, path=f"{path}.assign.{name}"))
            if "when" in rule:
                issues.extend(_expr_reference_issues(rule["when"], fields=fields, externs=externs, path=f"{path}.when"))
        for index, rule in enumerate(step_rules):
            path = f"step_rules[{index}]"
            if not isinstance(rule, Mapping):
                issues.append(issue("schema", path, "rule must be a mapping"))
                continue
            for name, expr in dict(rule.get("assign", {})).items():
                if str(name) not in outputs_names:
                    issues.append(issue("schema", f"{path}.assign.{name}", f"unknown output {name!r}"))
                issues.extend(_expr_reference_issues(expr, fields=fields, externs=externs, path=f"{path}.assign.{name}"))
            for name, expr in dict(rule.get("state_updates", {})).items():
                if str(name) not in set(str(item) for item in state):
                    issues.append(issue("schema", f"{path}.state_updates.{name}", f"unknown state {name!r}"))
                issues.extend(_expr_reference_issues(expr, fields=fields, externs=externs, path=f"{path}.state_updates.{name}"))
            if "when" in rule:
                issues.extend(_expr_reference_issues(rule["when"], fields=fields, externs=externs, path=f"{path}.when"))
        return issues

    def context(self, ir: Mapping[str, Any]) -> CheckContext:
        symbols: dict[str, Symbol] = {}
        for collection_name, kind, direction in (
            ("inputs", "field", "input"),
            ("state", "state", "internal"),
            ("outputs", "field", "output"),
        ):
            collection = ir.get(collection_name, {})
            if collection is None:
                collection = {}
            if not isinstance(collection, Mapping):
                continue
            for name, spec in collection.items():
                spec_map = spec if isinstance(spec, Mapping) else {"type": spec}
                symbols[str(name)] = Symbol(
                    name=str(name),
                    kind=kind,
                    type=type_from_spec(spec_map),
                    direction=direction,
                    roles=(direction,),
                    source=f"ref_model_ir.{collection_name}",
                )
        extern_map = ir.get("externs", {})
        if not isinstance(extern_map, Mapping):
            extern_map = {}
        externs = tuple(
            ExternCheck(extern_id=str(name), spec=spec if isinstance(spec, Mapping) else {}, path=f"externs.{name}")
            for name, spec in extern_map.items()
        )
        assignments: list[AssignmentCheck] = []
        predicates: list[PredicateCheck] = []
        rules = [rule for rule in ir.get("rules", ()) or () if isinstance(rule, Mapping)]
        step_rules = [rule for rule in ir.get("step_rules", ()) or () if isinstance(rule, Mapping)]
        for index, rule in enumerate(rules):
            path = f"rules[{index}]"
            if "when" in rule:
                predicates.append(PredicateCheck(path=f"{path}.when", expr=rule.get("when"), rule_id=str(rule.get("id") or "")))
            for name, expr in dict(rule.get("assign", {})).items():
                assignments.append(
                    AssignmentCheck(
                        path=f"{path}.assign.{name}",
                        target={"kind": "field", "name": str(name)},
                        value=expr,
                        condition=rule.get("when"),
                        rule_id=str(rule.get("id") or ""),
                    )
                )
        for index, rule in enumerate(step_rules):
            path = f"step_rules[{index}]"
            if "when" in rule:
                predicates.append(PredicateCheck(path=f"{path}.when", expr=rule.get("when"), rule_id=str(rule.get("id") or "")))
            for name, expr in dict(rule.get("assign", {})).items():
                assignments.append(
                    AssignmentCheck(
                        path=f"{path}.assign.{name}",
                        target={"kind": "field", "name": str(name)},
                        value=expr,
                        condition=rule.get("when"),
                        rule_id=str(rule.get("id") or ""),
                    )
                )
            for name, expr in dict(rule.get("state_updates", {})).items():
                assignments.append(
                    AssignmentCheck(
                        path=f"{path}.state_updates.{name}",
                        target={"kind": "state", "name": str(name)},
                        value=expr,
                        condition=rule.get("when"),
                        rule_id=str(rule.get("id") or ""),
                    )
                )
        totality = tuple(
            TotalityCheck(path=f"outputs.{name}", output=str(name), rules=tuple(rules + step_rules))
            for name in dict(ir.get("outputs", {}))
        )
        equivalences = tuple(_equivalence_checks(ir, self.ref_model_plan or {}))
        return CheckContext(
            target=str(ir.get("target") or ""),
            symbols=symbols,
            assignments=tuple(assignments),
            predicates=tuple(predicates),
            totality=totality,
            equivalences=equivalences,
            externs=externs,
            metadata={
                "schema_version": ir.get("schema_version"),
                "target": ir.get("target"),
                "subject": "RefModelIR",
                "rule_ids": tuple(str(rule.get("id") or "") for rule in rules),
                "step_rule_ids": tuple(str(rule.get("id") or "") for rule in step_rules),
                "ref_model_ir_rules": tuple(dict(rule) for rule in rules),
                "ref_model_ir_step_rules": tuple(dict(rule) for rule in step_rules),
                "ref_model_plan": dict(self.ref_model_plan or {}),
            },
        )


def _symbols_from_context(value: Any) -> dict[str, Symbol]:
    result: dict[str, Symbol] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        roles = item.get("roles", ())
        result[name] = Symbol(
            name=name,
            kind=str(item.get("kind") or "signal"),
            type=type_from_spec(item.get("type", {})),
            direction=str(item.get("direction")) if item.get("direction") is not None else None,
            roles=tuple(str(role) for role in roles) if isinstance(roles, list | tuple) else (),
            source=str(item.get("source")) if item.get("source") is not None else None,
        )
    return result


def _semantic_ast_checks(
    ast: Mapping[str, Any],
    path: str,
    expressions: list[ExpressionCheck],
    assignments: list[AssignmentCheck],
    predicates: list[PredicateCheck],
    *,
    rule_id: str | None,
    allow_smt: bool = True,
) -> None:
    node = str(ast.get("node") or "")
    if node in {"assignment", "constant_relation"}:
        assignments.append(AssignmentCheck(path=path, target=ast.get("target"), value=ast.get("value"), rule_id=rule_id))
        return
    if node == "conditional_assignment":
        assignments.append(
            AssignmentCheck(
                path=path,
                target=ast.get("target"),
                value=ast.get("value"),
                condition=ast.get("condition"),
                rule_id=rule_id,
            )
        )
        return
    if node == "constraint":
        predicates.append(PredicateCheck(path=path, expr=ast.get("expr"), rule_id=rule_id))
        return
    if node in {"compare", "implication"}:
        if allow_smt:
            predicates.append(PredicateCheck(path=path, expr=ast, rule_id=rule_id))
        else:
            expressions.append(ExpressionCheck(path=path, expr=ast, rule_id=rule_id))
        return
    if node == "operation_relation":
        for index, operand in enumerate(ast.get("operands", []) if isinstance(ast.get("operands"), list) else []):
            expressions.append(ExpressionCheck(path=f"{path}.operands[{index}]", expr=operand, rule_id=rule_id))
        if "result" in ast:
            expressions.append(ExpressionCheck(path=f"{path}.result", expr=ast.get("result"), rule_id=rule_id))
        return
    if node in {"temporal_rule", "protocol_rule"}:
        for key, value in ast.items():
            if key == "node":
                continue
            if isinstance(value, Mapping):
                _semantic_ast_checks(
                    value,
                    f"{path}.{key}",
                    expressions,
                    assignments,
                    predicates,
                    rule_id=rule_id,
                    allow_smt=False,
                )
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        _semantic_ast_checks(
                            item,
                            f"{path}.{key}[{index}]",
                            expressions,
                            assignments,
                            predicates,
                            rule_id=rule_id,
                            allow_smt=False,
                        )
        return
    if node in {"reset_rule", "sequential_update", "fsm", "state_transition"}:
        for key, value in ast.items():
            if key == "node":
                continue
            if isinstance(value, Mapping):
                _semantic_ast_checks(value, f"{path}.{key}", expressions, assignments, predicates, rule_id=rule_id, allow_smt=allow_smt)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        _semantic_ast_checks(item, f"{path}.{key}[{index}]", expressions, assignments, predicates, rule_id=rule_id, allow_smt=allow_smt)
        return
    for key, value in ast.items():
        if key == "node":
            continue
        if isinstance(value, Mapping):
            _semantic_ast_checks(value, f"{path}.{key}", expressions, assignments, predicates, rule_id=rule_id, allow_smt=allow_smt)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    _semantic_ast_checks(item, f"{path}.{key}[{index}]", expressions, assignments, predicates, rule_id=rule_id, allow_smt=allow_smt)


def _expr_reference_issues(
    expr: Any,
    *,
    fields: set[str],
    externs: set[str],
    path: str,
) -> list[CheckIssue]:
    issues: list[CheckIssue] = []
    if isinstance(expr, Mapping):
        if "field" in expr or expr.get("kind") == "field":
            name = _field_name(expr.get("field", expr))
            if name not in fields:
                issues.append(issue("schema", path, f"unknown field {name!r}"))
        if "state" in expr or expr.get("kind") == "state":
            name = _field_name(expr.get("state", expr))
            if name not in fields:
                issues.append(issue("schema", path, f"unknown field {name!r}"))
        if "extern_call" in expr and str(expr["extern_call"]) not in externs:
            issues.append(issue("schema", path, f"unknown extern {expr['extern_call']!r}", extern_id=str(expr["extern_call"])))
        for key, value in expr.items():
            if key in {"literal", "kind", "op", "type", "name", "encoding", "result"}:
                continue
            if isinstance(value, Mapping):
                issues.extend(_expr_reference_issues(value, fields=fields, externs=externs, path=f"{path}.{key}"))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    issues.extend(_expr_reference_issues(item, fields=fields, externs=externs, path=f"{path}.{key}[{index}]"))
    elif isinstance(expr, list):
        for index, item in enumerate(expr):
            issues.extend(_expr_reference_issues(item, fields=fields, externs=externs, path=f"{path}[{index}]"))
    return issues


def _equivalence_checks(ir: Mapping[str, Any], plan: Mapping[str, Any]) -> list[EquivalenceCheck]:
    if not plan:
        return []
    result: list[EquivalenceCheck] = []
    rules = [rule for rule in (*(ir.get("rules", ()) or ()), *(ir.get("step_rules", ()) or ())) if isinstance(rule, Mapping)]
    for plan_rule in plan.get("rules", ()):
        if not isinstance(plan_rule, Mapping):
            continue
        rule_id = str(plan_rule.get("id") or "")
        target, plan_expr, plan_condition = _plan_rule_expr(plan_rule, ir)
        if target is None or plan_expr is None:
            continue
        ir_rule = _matching_ir_rule(rules, rule_id)
        if ir_rule is None:
            result.append(
                EquivalenceCheck(
                    path="rules",
                    left={"literal": 0},
                    right={"literal": 1},
                    rule_id=rule_id,
                )
            )
            continue
        ir_expr = dict(ir_rule.get("assign", {})).get(target)
        if ir_expr is None and target in dict(ir.get("state", {})):
            ir_expr = dict(ir_rule.get("state_updates", {})).get(target)
        if ir_expr is None:
            result.append(
                EquivalenceCheck(
                    path=f"rules.{rule_id}",
                    left={"literal": 0},
                    right={"literal": 1},
                    rule_id=rule_id,
                )
            )
            continue
        trusted_extern = _trusted_operation_extern(plan_rule, ir_expr, ir)
        condition = plan_condition
        if "when" in ir_rule:
            condition = {"and": [condition, ir_rule.get("when")]} if condition is not None else ir_rule.get("when")
        result.append(
            EquivalenceCheck(
                path=f"rules.{rule_id}",
                left=plan_expr,
                right=ir_expr,
                condition=condition,
                rule_id=rule_id or str(ir_rule.get("id") or "rule"),
                trusted_extern_id=trusted_extern,
            )
        )
    return result


def _plan_rule_expr(rule: Mapping[str, Any], ir: Mapping[str, Any]) -> tuple[str | None, Any, Any]:
    kind = str(rule.get("kind") or "")
    if kind in {"assignment", "constant_relation"}:
        return _target_name(rule.get("target")), rule.get("value"), None
    if kind == "conditional_assignment":
        return _target_name(rule.get("target")), rule.get("value"), rule.get("condition")
    if kind == "operation_relation":
        result = rule.get("result", {"kind": "field", "name": "expected"})
        return _target_name(result), {"operation": rule.get("operation"), "operands": rule.get("operands", ())}, None
    if kind == "state_transition":
        target = _target_name(rule.get("state") or rule.get("state_signal"))
        if target is None:
            states = list(dict(ir.get("state", {})))
            target = str(states[0]) if len(states) == 1 else None
        if target is None:
            return None, None, None
        condition = rule.get("condition")
        state_condition: Any = {"eq": [{"state": target}, rule.get("from")]}
        if condition is not None:
            state_condition = {"and": [state_condition, condition]}
        return target, rule.get("to"), state_condition
    return None, None, None


def _matching_ir_rule(rules: list[Mapping[str, Any]], plan_rule_id: str) -> Mapping[str, Any] | None:
    if plan_rule_id:
        for rule in rules:
            if rule.get("source_rule_id") == plan_rule_id or rule.get("id") == plan_rule_id:
                return rule
    return rules[0] if len(rules) == 1 else None


def _trusted_operation_extern(plan_rule: Mapping[str, Any], ir_expr: Any, ir: Mapping[str, Any]) -> str | None:
    if str(plan_rule.get("kind") or "") != "operation_relation" or not isinstance(ir_expr, Mapping):
        return None
    extern_id = ir_expr.get("extern_call")
    if not extern_id:
        return None
    extern = dict(dict(ir.get("externs", {})).get(str(extern_id), {}))
    return str(extern_id) if extern.get("verification_policy") == "trusted_standard" else None


def _target_name(expr: Any) -> str | None:
    if isinstance(expr, Mapping):
        if expr.get("kind") in {"field", "signal", "state"}:
            return str(expr.get("name") or "")
        if "field" in expr:
            return _field_name(expr.get("field"))
        if "state" in expr:
            return _field_name(expr.get("state"))
    return None


def _field_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("field") or value.get("state") or "")
    return str(value)
