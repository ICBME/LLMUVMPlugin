"""Verifier for RefModelIR schemas and formal obligations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .extern import ExternRegistry, PYTHON_STDLIB_ALLOWLIST
from .schema import (
    REF_MODEL_IR_SCHEMA_VERSION,
    VerificationIssue,
    VerificationReport,
    normalize_extern_path,
    normalize_ref_model_ir,
)


def verify_ref_model_ir(
    ref_model_ir: Mapping[str, Any],
    *,
    ref_model_plan: Mapping[str, Any] | None = None,
    base_dir: str | Path = ".",
) -> VerificationReport:
    issues: list[VerificationIssue] = []
    proved_rules: list[str] = []
    trusted_externs: list[str] = []
    tested_externs: list[str] = []
    try:
        import z3  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - mandatory dependency should become a stable report
        return VerificationReport(
            status="failed",
            verification_level="unverified",
            issues=(
                VerificationIssue(
                    stage="z3",
                    message=f"z3-solver is required for RefModelIR verification: {exc}",
                ),
            ),
        )

    try:
        ir = normalize_ref_model_ir(ref_model_ir)
    except Exception as exc:  # noqa: BLE001
        return VerificationReport(
            status="failed",
            verification_level="unverified",
            issues=(VerificationIssue(stage="schema", message=str(exc)),),
        )

    issues.extend(_schema_issues(ir))
    extern_summary = _extern_issues(ir, base_dir=Path(base_dir))
    issues.extend(extern_summary["issues"])
    trusted_externs.extend(extern_summary["trusted"])
    tested_externs.extend(extern_summary["tested"])
    if not any(issue.blocking for issue in issues):
        totality = _z3_totality_and_overlap_issues(ir)
        issues.extend(totality["issues"])
        proved_rules.extend(totality["proved_rules"])
    if ref_model_plan is not None and not any(issue.blocking for issue in issues):
        equivalence = _z3_plan_equivalence_issues(ir, ref_model_plan)
        issues.extend(equivalence["issues"])
        proved_rules.extend(equivalence["proved_rules"])
        trusted_externs.extend(item for item in equivalence["trusted"] if item not in trusted_externs)

    blocked = [issue for issue in issues if issue.blocking]
    if blocked:
        status = "failed"
        level = "unverified"
    elif trusted_externs and proved_rules:
        status = "passed"
        level = "mixed_formal_trusted"
    elif trusted_externs:
        status = "passed"
        level = "trusted_standard"
    elif proved_rules:
        status = "passed"
        level = "formally_verified"
    else:
        status = "passed"
        level = "schema_verified"
    return VerificationReport(
        status=status,
        verification_level=level,
        proved_rules=tuple(dict.fromkeys(proved_rules)),
        trusted_standard_externs=tuple(dict.fromkeys(trusted_externs)),
        tested_externs=tuple(dict.fromkeys(tested_externs)),
        issues=tuple(issues),
        metadata={"schema_version": ir.get("schema_version"), "target": ir.get("target")},
    )


def _schema_issues(ir: Mapping[str, Any]) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    if ir.get("schema_version") != REF_MODEL_IR_SCHEMA_VERSION:
        issues.append(_issue("schema", "schema_version", "unsupported RefModelIR schema_version"))
    if not str(ir.get("target") or "").strip():
        issues.append(_issue("schema", "target", "RefModelIR must define target"))
    inputs = ir.get("inputs")
    outputs = ir.get("outputs")
    rules = ir.get("rules")
    step_rules = ir.get("step_rules", [])
    state = ir.get("state", {})
    extern_map = ir.get("externs", {})
    if not isinstance(inputs, Mapping):
        issues.append(_issue("schema", "inputs", "RefModelIR inputs must be a mapping"))
        inputs = {}
    if not isinstance(outputs, Mapping) or not outputs:
        issues.append(_issue("schema", "outputs", "RefModelIR outputs must be a non-empty mapping"))
        outputs = {}
    if state is None:
        state = {}
    if not isinstance(state, Mapping):
        issues.append(_issue("schema", "state", "RefModelIR state must be a mapping"))
        state = {}
    if extern_map is None:
        extern_map = {}
    if not isinstance(extern_map, Mapping):
        issues.append(_issue("schema", "externs", "RefModelIR externs must be a mapping"))
        extern_map = {}
    if not isinstance(rules, list):
        issues.append(_issue("schema", "rules", "RefModelIR rules must be a list"))
        rules = []
    if step_rules is None:
        step_rules = []
    if not isinstance(step_rules, list):
        issues.append(_issue("schema", "step_rules", "RefModelIR step_rules must be a list"))
        step_rules = []
    if not rules and not step_rules:
        issues.append(_issue("schema", "rules", "RefModelIR must define rules or step_rules"))
    state_names = set(str(name) for name in state)
    fields = set(str(name) for name in inputs) | state_names
    output_names = set(str(name) for name in outputs)
    externs = set(str(name) for name in extern_map)
    for collection_name, collection in (("inputs", inputs), ("state", state), ("outputs", outputs)):
        for name, spec in collection.items():
            if not isinstance(spec, Mapping):
                continue
            typ = str(spec.get("type") or "")
            if typ == "enum" and not spec.get("choices"):
                issues.append(_issue("schema", f"{collection_name}.{name}.choices", "enum fields must define choices"))
            if typ == "bitvector":
                width = spec.get("width")
                if not isinstance(width, int) or width <= 0:
                    issues.append(_issue("schema", f"{collection_name}.{name}.width", "bitvector fields must define a positive width"))
    for index, rule in enumerate(rules):
        path = f"rules[{index}]"
        if not isinstance(rule, Mapping):
            issues.append(_issue("schema", path, "rule must be a mapping"))
            continue
        assign = rule.get("assign")
        if not isinstance(assign, Mapping) or not assign:
            issues.append(_issue("schema", f"{path}.assign", "rule must assign at least one output"))
            continue
        for name, expr in assign.items():
            if str(name) not in output_names:
                issues.append(_issue("schema", f"{path}.assign.{name}", f"unknown output {name!r}"))
            issues.extend(_expr_reference_issues(expr, fields=fields, externs=externs, path=f"{path}.assign.{name}"))
        if "when" in rule:
            issues.extend(_expr_reference_issues(rule["when"], fields=fields, externs=externs, path=f"{path}.when"))
    for index, rule in enumerate(step_rules):
        if isinstance(rule, Mapping):
            for name, expr in dict(rule.get("assign", {})).items():
                if str(name) not in output_names:
                    issues.append(_issue("schema", f"step_rules[{index}].assign.{name}", f"unknown output {name!r}"))
                issues.extend(
                    _expr_reference_issues(
                        expr,
                        fields=fields,
                        externs=externs,
                        path=f"step_rules[{index}].assign.{name}",
                    )
                )
            for name, expr in dict(rule.get("state_updates", {})).items():
                if str(name) not in state_names:
                    issues.append(_issue("schema", f"step_rules[{index}].state_updates.{name}", f"unknown state {name!r}"))
                issues.extend(
                    _expr_reference_issues(
                        expr,
                        fields=fields,
                        externs=externs,
                        path=f"step_rules[{index}].state_updates.{name}",
                    )
                )
    return issues


def _expr_reference_issues(
    expr: Any,
    *,
    fields: set[str],
    externs: set[str],
    path: str,
) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    if isinstance(expr, Mapping):
        if "field" in expr or expr.get("kind") == "field":
            name = _field_name(expr.get("field", expr))
            if name not in fields:
                issues.append(_issue("schema", path, f"unknown field {name!r}"))
        if "extern_call" in expr and str(expr["extern_call"]) not in externs:
            issues.append(_issue("schema", path, f"unknown extern {expr['extern_call']!r}"))
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


def _extern_issues(ir: Mapping[str, Any], *, base_dir: Path) -> dict[str, Any]:
    issues: list[VerificationIssue] = []
    trusted: list[str] = []
    tested: list[str] = []
    externs = ir.get("externs", {})
    if externs is None:
        return {"issues": issues, "trusted": trusted, "tested": tested}
    if not isinstance(externs, Mapping):
        return {"issues": [_issue("extern", "externs", "externs must be a mapping")], "trusted": trusted, "tested": tested}
    for extern_id, spec in externs.items():
        path = f"externs.{extern_id}"
        if not isinstance(spec, Mapping):
            issues.append(_issue("extern", path, "extern spec must be a mapping", extern_id=str(extern_id)))
            continue
        kind = str(spec.get("kind") or "")
        policy = str(spec.get("verification_policy") or "")
        issues.extend(_purity_issues(str(extern_id), spec, path=path))
        if kind == "python_stdlib":
            binding = str(spec.get("binding") or extern_id)
            if binding not in PYTHON_STDLIB_ALLOWLIST:
                issues.append(_issue("extern", f"{path}.binding", f"python stdlib binding {binding!r} is not allowlisted", extern_id=str(extern_id)))
            if policy != "trusted_standard":
                issues.append(_issue("extern", f"{path}.verification_policy", "python_stdlib externs must use trusted_standard policy", extern_id=str(extern_id)))
            for key in ("standard_name", "implementation", "version", "artifact_sha256"):
                if not str(spec.get(key) or "").strip():
                    issues.append(_issue("extern", f"{path}.{key}", f"trusted_standard extern must define {key}", extern_id=str(extern_id)))
            if not any(issue.extern_id == str(extern_id) and issue.blocking for issue in issues):
                trusted.append(str(extern_id))
            continue
        if kind == "c_abi":
            issues.extend(_c_abi_issues(str(extern_id), spec, path=path, base_dir=base_dir))
            if policy == "formal_model" and not any(issue.extern_id == str(extern_id) and issue.blocking for issue in issues):
                if _run_c_abi_conformance(str(extern_id), spec, base_dir=base_dir, issues=issues):
                    tested.append(str(extern_id))
            continue
        if kind == "systemc_worker":
            issues.append(_issue("extern", path, "systemc_worker externs are declared but not implemented in v1", extern_id=str(extern_id)))
            continue
        issues.append(_issue("extern", path, f"unsupported extern kind {kind!r}", extern_id=str(extern_id)))
    return {"issues": issues, "trusted": trusted, "tested": tested}


def _c_abi_issues(
    extern_id: str,
    spec: Mapping[str, Any],
    *,
    path: str,
    base_dir: Path,
) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    policy = str(spec.get("verification_policy") or "")
    try:
        library = normalize_extern_path(spec.get("library"))
    except Exception as exc:  # noqa: BLE001
        issues.append(_issue("extern", f"{path}.library", str(exc), extern_id=extern_id))
        library = ""
    if not str(spec.get("function") or "").strip():
        issues.append(_issue("extern", f"{path}.function", "c_abi extern must define function", extern_id=extern_id))
    if not str(spec.get("artifact_sha256") or "").strip():
        issues.append(_issue("extern", f"{path}.artifact_sha256", "c_abi extern must define artifact_sha256", extern_id=extern_id))
    elif library:
        lib_path = base_dir / library
        if lib_path.exists():
            actual = hashlib.sha256(lib_path.read_bytes()).hexdigest()
            if actual != str(spec.get("artifact_sha256")):
                issues.append(_issue("extern", f"{path}.artifact_sha256", "c_abi artifact_sha256 mismatch", extern_id=extern_id))
    timeout_ms = spec.get("timeout_ms")
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        issues.append(_issue("extern", f"{path}.timeout_ms", "c_abi extern must define a positive timeout_ms", extern_id=extern_id))
    if policy != "formal_model":
        issues.append(_issue("extern", f"{path}.verification_policy", "non-standard c_abi externs require formal_model policy", extern_id=extern_id))
    if "formal_model" not in spec:
        issues.append(_issue("extern", f"{path}.formal_model", "c_abi formal_model policy requires formal_model expression", extern_id=extern_id))
    return issues


def _purity_issues(extern_id: str, spec: Mapping[str, Any], *, path: str) -> list[VerificationIssue]:
    issues: list[VerificationIssue] = []
    if spec.get("pure") is not True:
        issues.append(_issue("extern", f"{path}.pure", "extern must declare pure=true", extern_id=extern_id))
    if spec.get("deterministic") is not True:
        issues.append(_issue("extern", f"{path}.deterministic", "extern must declare deterministic=true", extern_id=extern_id))
    return issues


def _run_c_abi_conformance(
    extern_id: str,
    spec: Mapping[str, Any],
    *,
    base_dir: Path,
    issues: list[VerificationIssue],
) -> bool:
    cases = spec.get("conformance_cases", ())
    if not cases:
        issues.append(_issue("extern", f"externs.{extern_id}.conformance_cases", "c_abi extern requires conformance_cases", extern_id=extern_id))
        return False
    registry = ExternRegistry({extern_id: spec}, base_dir=base_dir)
    ok = True
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            continue
        try:
            actual = registry.call(extern_id, list(case.get("args", ())))
        except Exception as exc:  # noqa: BLE001
            issues.append(_issue("extern", f"externs.{extern_id}.conformance_cases[{index}]", f"{type(exc).__name__}: {exc}", extern_id=extern_id))
            ok = False
            continue
        if actual != case.get("expected"):
            issues.append(_issue("extern", f"externs.{extern_id}.conformance_cases[{index}]", f"expected {case.get('expected')!r}, got {actual!r}", extern_id=extern_id))
            ok = False
    return ok


def _z3_totality_and_overlap_issues(ir: Mapping[str, Any]) -> dict[str, Any]:
    import z3

    env = _z3_env(ir)
    issues: list[VerificationIssue] = []
    proved_rules: list[str] = []
    rules = [
        rule
        for rule in (*(ir.get("rules", ()) or ()), *(ir.get("step_rules", ()) or ()))
        if isinstance(rule, Mapping)
    ]
    constraints = _input_constraints(ir, env)
    outputs = set(str(name) for name in dict(ir.get("outputs", {})))
    for output in outputs:
        assigning = [rule for rule in rules if output in dict(rule.get("assign", {}))]
        if not assigning:
            issues.append(_issue("z3", f"outputs.{output}", f"output {output!r} is never assigned"))
            continue
        conditions = [_z3_condition(rule.get("when"), env, ir) for rule in assigning]
        solver = z3.Solver()
        solver.add(*constraints)
        solver.add(z3.Not(z3.Or(*conditions)))
        if solver.check() == z3.sat:
            issues.append(_issue("z3", f"outputs.{output}", f"output {output!r} is not assigned for all inputs"))
        else:
            proved_rules.extend(str(rule.get("id") or f"rule_{idx}") for idx, rule in enumerate(assigning))
        for left_index, left in enumerate(assigning):
            for right in assigning[left_index + 1 :]:
                solver = z3.Solver()
                solver.add(*constraints)
                solver.add(_z3_condition(left.get("when"), env, ir))
                solver.add(_z3_condition(right.get("when"), env, ir))
                if solver.check() == z3.sat:
                    issues.append(
                        _issue(
                            "z3",
                            f"outputs.{output}",
                            f"rules {left.get('id')!r} and {right.get('id')!r} can both assign {output!r}",
                        )
                    )
    return {"issues": issues, "proved_rules": proved_rules}


def _z3_plan_equivalence_issues(ir: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    import z3

    env = _z3_env(ir)
    constraints = _input_constraints(ir, env)
    issues: list[VerificationIssue] = []
    proved_rules: list[str] = []
    trusted: list[str] = []
    ir_rules = [
        rule
        for rule in (*(ir.get("rules", ()) or ()), *(ir.get("step_rules", ()) or ()))
        if isinstance(rule, Mapping)
    ]
    for plan_rule in plan.get("rules", ()):
        if not isinstance(plan_rule, Mapping):
            continue
        rule_id = str(plan_rule.get("id") or "")
        target, plan_expr, plan_condition = _plan_rule_expr(plan_rule, ir)
        if target is None or plan_expr is None:
            continue
        ir_rule = _matching_ir_rule(ir_rules, rule_id)
        if ir_rule is None:
            issues.append(_issue("z3", "rules", f"no RefModelIR rule matches RefModelPlan rule {rule_id!r}", rule_id=rule_id))
            continue
        ir_expr = dict(ir_rule.get("assign", {})).get(target)
        if ir_expr is None and target in dict(ir.get("state", {})):
            ir_expr = dict(ir_rule.get("state_updates", {})).get(target)
        if ir_expr is None:
            issues.append(_issue("z3", f"rules.{rule_id}", f"matching IR rule does not assign {target!r}", rule_id=rule_id))
            continue
        if _is_trusted_operation(plan_rule, ir_expr, ir):
            trusted.append(str(ir_expr.get("extern_call")))
            continue
        try:
            plan_z3 = _z3_expr(plan_expr, env, ir)
            ir_z3 = _z3_expr(ir_expr, env, ir)
            solver = z3.Solver()
            solver.add(*constraints)
            if plan_condition is not None:
                solver.add(_z3_expr(plan_condition, env, ir))
            if "when" in ir_rule:
                solver.add(_z3_condition(ir_rule.get("when"), env, ir))
            solver.add(plan_z3 != ir_z3)
            if solver.check() == z3.sat:
                issues.append(_issue("z3", f"rules.{rule_id}", "RefModelIR expression is not equivalent to RefModelPlan rule", rule_id=rule_id))
            else:
                proved_rules.append(rule_id or str(ir_rule.get("id") or "rule"))
        except Exception as exc:  # noqa: BLE001
            issues.append(_issue("z3", f"rules.{rule_id}", f"could not prove rule equivalence: {exc}", rule_id=rule_id))
    return {"issues": issues, "proved_rules": proved_rules, "trusted": trusted}


def _z3_env(ir: Mapping[str, Any]) -> dict[str, Any]:
    import z3

    env: dict[str, Any] = {}
    for name, spec in {**dict(ir.get("inputs", {})), **dict(ir.get("state", {}))}.items():
        typ = str(dict(spec).get("type") if isinstance(spec, Mapping) else spec)
        if typ in {"bool", "boolean"}:
            env[str(name)] = z3.Bool(str(name))
        elif typ in {"int", "uint"}:
            env[str(name)] = z3.Int(str(name))
        elif typ == "bitvector":
            width = int(dict(spec).get("width", 32)) if isinstance(spec, Mapping) else 32
            env[str(name)] = z3.BitVec(str(name), width)
        else:
            env[str(name)] = z3.String(str(name))
    return env


def _input_constraints(ir: Mapping[str, Any], env: Mapping[str, Any]) -> list[Any]:
    import z3

    constraints = []
    for name, spec in {**dict(ir.get("inputs", {})), **dict(ir.get("state", {}))}.items():
        if not isinstance(spec, Mapping):
            continue
        var = env[str(name)]
        typ = str(spec.get("type") or "")
        if typ == "enum":
            constraints.append(z3.Or(*(var == str(choice) for choice in spec.get("choices", ()))))
        if typ in {"int", "uint"}:
            if spec.get("min") is not None:
                constraints.append(var >= int(spec["min"]))
            if spec.get("max") is not None:
                constraints.append(var <= int(spec["max"]))
    return constraints


def _z3_condition(expr: Any, env: Mapping[str, Any], ir: Mapping[str, Any]) -> Any:
    import z3

    if expr is None:
        return z3.BoolVal(True)
    value = _z3_expr(expr, env, ir)
    return value if hasattr(value, "sort") and value.sort().kind() == z3.Z3_BOOL_SORT else value == True  # noqa: E712


def _z3_expr(expr: Any, env: Mapping[str, Any], ir: Mapping[str, Any]) -> Any:
    import z3

    if isinstance(expr, bool):
        return z3.BoolVal(expr)
    if isinstance(expr, int):
        return z3.IntVal(expr)
    if isinstance(expr, str):
        return z3.StringVal(expr)
    if not isinstance(expr, Mapping):
        return z3.StringVal(str(expr))
    if "literal" in expr:
        return _z3_expr(expr["literal"], env, ir)
    if expr.get("kind") == "literal":
        return _z3_expr(expr.get("value"), env, ir)
    if "field" in expr or expr.get("kind") == "field":
        return env[_field_name(expr.get("field", expr))]
    if "state" in expr or expr.get("kind") == "state":
        return env[_field_name(expr.get("state", expr))]
    if "eq" in expr:
        left, right = expr["eq"]
        return _z3_expr(left, env, ir) == _z3_expr(right, env, ir)
    if expr.get("kind") == "compare" or "compare" in expr:
        spec = expr.get("compare", expr)
        left = _z3_expr(spec.get("left"), env, ir)
        right = _z3_expr(spec.get("right"), env, ir)
        op = str(spec.get("op"))
        if op in {"==", "eq"}:
            return left == right
        if op in {"!=", "ne"}:
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    if "not" in expr or expr.get("kind") == "unary_op":
        op = str(expr.get("op") or ("not" if "not" in expr else ""))
        value = _z3_expr(expr.get("not", expr.get("operand")), env, ir)
        if op in {"not", "!"}:
            return z3.Not(value)
        if op in {"bit_not", "~"}:
            return ~value
    if "and" in expr:
        return z3.And(*(_z3_expr(item, env, ir) for item in expr["and"]))
    if "or" in expr:
        return z3.Or(*(_z3_expr(item, env, ir) for item in expr["or"]))
    if expr.get("kind") == "binary_op" or "binary" in expr:
        spec = expr.get("binary", expr)
        left = _z3_expr(spec.get("left"), env, ir)
        right = _z3_expr(spec.get("right"), env, ir)
        op = str(spec.get("op"))
        if op in {"and", "&&"}:
            return z3.And(left, right)
        if op in {"or", "||"}:
            return z3.Or(left, right)
        if op in {"xor", "^"}:
            return left ^ right
        if op in {"add", "+"}:
            return left + right
        if op in {"sub", "-"}:
            return left - right
        if op in {"mul", "*"}:
            return left * right
    if "if" in expr or expr.get("kind") == "mux":
        spec = expr.get("if", expr)
        cond = spec.get("cond", spec.get("condition"))
        when_true = spec.get("then", spec.get("when_true"))
        when_false = spec.get("else", spec.get("when_false"))
        return z3.If(_z3_condition(cond, env, ir), _z3_expr(when_true, env, ir), _z3_expr(when_false, env, ir))
    if "extern_call" in expr:
        extern_id = str(expr["extern_call"])
        extern = dict(dict(ir.get("externs", {})).get(extern_id, {}))
        if extern.get("verification_policy") == "formal_model" and "formal_model" in extern:
            local_env = dict(env)
            for index, arg in enumerate(expr.get("args", ())):
                local_env[f"arg{index}"] = _z3_expr(arg, env, ir)
            return _z3_expr(extern["formal_model"], local_env, ir)
        raise ValueError(f"extern {extern_id!r} has no SMT formal_model")
    raise ValueError(f"unsupported SMT expression {expr!r}")


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


def _is_trusted_operation(plan_rule: Mapping[str, Any], ir_expr: Any, ir: Mapping[str, Any]) -> bool:
    if str(plan_rule.get("kind") or "") != "operation_relation" or not isinstance(ir_expr, Mapping):
        return False
    extern_id = ir_expr.get("extern_call")
    if not extern_id:
        return False
    extern = dict(dict(ir.get("externs", {})).get(str(extern_id), {}))
    return extern.get("verification_policy") == "trusted_standard"


def _target_name(expr: Any) -> str | None:
    if isinstance(expr, Mapping):
        if expr.get("kind") in {"field", "signal", "state"}:
            return str(expr.get("name") or "")
        if "field" in expr:
            return _field_name(expr.get("field"))
    return None


def _field_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("field") or value.get("state") or "")
    return str(value)


def _issue(
    stage: str,
    path: str,
    message: str,
    *,
    rule_id: str | None = None,
    extern_id: str | None = None,
) -> VerificationIssue:
    return VerificationIssue(stage=stage, path=path, message=message, rule_id=rule_id, extern_id=extern_id)


__all__ = ["verify_ref_model_ir"]
