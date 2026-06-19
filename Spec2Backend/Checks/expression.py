"""Expression type inference and local SMT translation for checker adapters."""

from __future__ import annotations

from typing import Any, Mapping

from .model import (
    ANY,
    BOOL,
    INT,
    STRING,
    CheckIssue,
    Symbol,
    TypeSpec,
    issue,
    merge_numeric,
    type_compatible,
    type_from_spec,
)


BOOL_UNARY_OPS = {"logical_not", "not", "reduction_not", "!"}
BIT_UNARY_OPS = {"bitwise_not", "bit_not", "~", "neg"}
BOOL_BINARY_OPS = {"and", "or", "logical_and", "logical_or", "&&", "||"}
ARITH_BINARY_OPS = {
    "add",
    "sub",
    "mul",
    "div",
    "mod",
    "+",
    "-",
    "*",
    "/",
    "%",
}
BIT_BINARY_OPS = {
    "bitwise_and",
    "bitwise_or",
    "bitwise_xor",
    "xor",
    "shift_left",
    "shift_right",
    "&",
    "|",
    "^",
    "<<",
    ">>",
}


class ExpressionChecker:
    def __init__(self, symbols: Mapping[str, Symbol], externs: Mapping[str, Mapping[str, Any]] | None = None):
        self.symbols = symbols
        self.externs = externs or {}

    def infer(self, expr: Any, path: str) -> tuple[TypeSpec, list[CheckIssue]]:
        issues: list[CheckIssue] = []
        typ = self._infer(expr, path, issues)
        return typ, issues

    def _infer(self, expr: Any, path: str, issues: list[CheckIssue]) -> TypeSpec:
        if isinstance(expr, bool):
            return BOOL
        if isinstance(expr, int):
            return INT
        if isinstance(expr, bytes):
            return TypeSpec("bytes")
        if isinstance(expr, str):
            return STRING
        if not isinstance(expr, Mapping):
            return ANY

        node = str(expr.get("node") or expr.get("kind") or "")
        if node in {"field_ref", "signal_ref", "state_ref", "field", "signal", "state"} or "field" in expr or "state" in expr:
            name = _ref_name(expr)
            symbol = self.symbols.get(name)
            if symbol is None:
                issues.append(issue("reference", path, f"unknown field {name!r}", code="unknown_symbol"))
                return ANY
            if symbol.type.is_any:
                issues.append(issue("type", path, f"symbol {name!r} has unresolved any type", code="any_type"))
            return symbol.type

        if node == "literal" or "literal" in expr:
            value = expr.get("value") if node == "literal" else expr.get("literal")
            if "type" in expr:
                return type_from_spec(expr)
            if isinstance(value, bool):
                return BOOL
            if isinstance(value, int):
                width = expr.get("width")
                if isinstance(width, int) and width > 0:
                    return TypeSpec("bitvector", width=width)
                return INT
            if isinstance(value, bytes):
                return TypeSpec("bytes")
            return STRING

        if node == "cast":
            if "value" in expr:
                self._infer(expr.get("value"), f"{path}.value", issues)
            target = type_from_spec({"type": expr.get("type", "any"), "width": expr.get("width")})
            return target

        if node == "compare" or "eq" in expr or "compare" in expr:
            if "eq" in expr:
                operands = expr.get("eq")
                left_expr, right_expr = operands if isinstance(operands, list | tuple) and len(operands) == 2 else (None, None)
            else:
                spec = expr.get("compare", expr)
                left_expr, right_expr = spec.get("left"), spec.get("right")
            left = self._infer(left_expr, f"{path}.left", issues)
            right = self._infer(right_expr, f"{path}.right", issues)
            if not type_compatible(left, right):
                issues.append(
                    issue(
                        "type",
                        path,
                        f"compare operands have incompatible types {left.to_json()} and {right.to_json()}",
                        code="compare_type_mismatch",
                    )
                )
            return BOOL

        if node == "unary_op" or "not" in expr:
            op = str(expr.get("op") or ("not" if "not" in expr else ""))
            operand_expr = expr.get("operand", expr.get("not"))
            operand = self._infer(operand_expr, f"{path}.operand", issues)
            if op in BOOL_UNARY_OPS and not (operand.is_bool or operand.is_any):
                issues.append(issue("type", path, "logical unary operand must be bool", code="condition_type"))
            if op in BIT_UNARY_OPS and not (operand.is_numeric or operand.is_any):
                issues.append(issue("type", path, "bitwise/arithmetic unary operand must be numeric", code="numeric_type"))
            return BOOL if op in BOOL_UNARY_OPS else operand

        if node == "binary_op" or "binary" in expr:
            spec = expr.get("binary", expr)
            op = str(spec.get("op") or "")
            left = self._infer(spec.get("left"), f"{path}.left", issues)
            right = self._infer(spec.get("right"), f"{path}.right", issues)
            if op in BOOL_BINARY_OPS:
                for side, typ in (("left", left), ("right", right)):
                    if not (typ.is_bool or typ.is_any):
                        issues.append(issue("type", f"{path}.{side}", "logical binary operand must be bool", code="condition_type"))
                return BOOL
            if op in ARITH_BINARY_OPS | BIT_BINARY_OPS:
                for side, typ in (("left", left), ("right", right)):
                    if not (typ.is_numeric or typ.is_any):
                        issues.append(issue("type", f"{path}.{side}", "binary operand must be numeric", code="numeric_type"))
                if left.kind == "bitvector" and right.kind == "bitvector" and left.width != right.width:
                    issues.append(issue("type", path, "bitvector operands must have matching widths", code="width_mismatch"))
                return merge_numeric(left, right)
            return ANY

        if "and" in expr:
            for index, item in enumerate(expr.get("and", ())):
                typ = self._infer(item, f"{path}.and[{index}]", issues)
                if not (typ.is_bool or typ.is_any):
                    issues.append(issue("type", f"{path}.and[{index}]", "and operands must be bool", code="condition_type"))
            return BOOL

        if "or" in expr:
            for index, item in enumerate(expr.get("or", ())):
                typ = self._infer(item, f"{path}.or[{index}]", issues)
                if not (typ.is_bool or typ.is_any):
                    issues.append(issue("type", f"{path}.or[{index}]", "or operands must be bool", code="condition_type"))
            return BOOL

        if node == "mux" or "if" in expr:
            spec = expr.get("if", expr)
            cond = self._infer(spec.get("condition", spec.get("cond")), f"{path}.condition", issues)
            if not (cond.is_bool or cond.is_any):
                issues.append(issue("type", f"{path}.condition", "mux condition must be bool", code="condition_type"))
            when_true = self._infer(spec.get("when_true", spec.get("then")), f"{path}.when_true", issues)
            when_false = self._infer(spec.get("when_false", spec.get("else")), f"{path}.when_false", issues)
            if not type_compatible(when_true, when_false):
                issues.append(issue("type", path, "mux branches must have compatible types", code="branch_type_mismatch"))
            return when_true if not when_true.is_any else when_false

        if node == "concat" or "concat" in expr:
            parts = expr.get("parts", expr.get("concat", ()))
            width = 0
            any_width = False
            if not isinstance(parts, list | tuple):
                return ANY
            for index, part in enumerate(parts):
                typ = self._infer(part, f"{path}.parts[{index}]", issues)
                if typ.kind == "bool":
                    width += 1
                elif typ.kind == "bitvector":
                    if typ.width is None:
                        any_width = True
                    else:
                        width += typ.width
                elif typ.is_any:
                    any_width = True
                else:
                    issues.append(issue("type", f"{path}.parts[{index}]", "concat parts must be bool or bitvector", code="concat_type"))
            return TypeSpec("bitvector", width=None if any_width else width)

        if node == "slice" or "slice" in expr:
            spec = expr.get("slice", expr)
            base = self._infer(spec.get("value"), f"{path}.value", issues)
            if not (base.kind == "bitvector" or base.is_any):
                issues.append(issue("type", f"{path}.value", "slice value must be bitvector", code="slice_type"))
            msb = spec.get("msb")
            lsb = spec.get("lsb")
            if isinstance(msb, int) and isinstance(lsb, int) and msb >= lsb:
                return TypeSpec("bitvector", width=msb - lsb + 1)
            return TypeSpec("bitvector")

        if node == "reduce":
            operand = self._infer(expr.get("operand"), f"{path}.operand", issues)
            if not (operand.kind == "bitvector" or operand.is_any):
                issues.append(issue("type", f"{path}.operand", "reduce operand must be bitvector", code="reduce_type"))
            return BOOL

        if node == "constraint":
            return self._infer(expr.get("expr"), f"{path}.expr", issues)

        if node == "implication":
            antecedent = self._infer(expr.get("antecedent"), f"{path}.antecedent", issues)
            consequent = self._infer(expr.get("consequent"), f"{path}.consequent", issues)
            if not (antecedent.is_bool or antecedent.is_any):
                issues.append(issue("type", f"{path}.antecedent", "implication antecedent must be bool", code="condition_type"))
            if not (consequent.is_bool or consequent.is_any):
                issues.append(issue("type", f"{path}.consequent", "implication consequent must be bool", code="condition_type"))
            return BOOL

        if node in {"clock_event", "rose", "fell", "stable", "past"}:
            signal = self._infer(expr.get("signal"), f"{path}.signal", issues)
            if not (signal.is_bool or signal.is_any):
                issues.append(issue("type", f"{path}.signal", f"{node} signal must be bool", code="event_signal_type"))
            return BOOL

        if node in {"assignment", "constant_relation"}:
            return self._infer(expr.get("value"), f"{path}.value", issues)

        if node == "conditional_assignment":
            condition = self._infer(expr.get("condition"), f"{path}.condition", issues)
            if not (condition.is_bool or condition.is_any):
                issues.append(issue("type", f"{path}.condition", "conditional assignment condition must be bool", code="condition_type"))
            return self._infer(expr.get("value"), f"{path}.value", issues)

        if "extern_call" in expr:
            extern_id = str(expr.get("extern_call") or "")
            if extern_id not in self.externs:
                issues.append(issue("reference", path, f"unknown extern {extern_id!r}", extern_id=extern_id, code="unknown_extern"))
                return ANY
            result = expr.get("result") or self.externs[extern_id].get("result_type")
            return type_from_spec(result) if result is not None else ANY

        if "decode_hex" in expr:
            return TypeSpec("bytes")
        if "encode_hex" in expr:
            return TypeSpec("string", format="hex")

        return ANY


def ref_name(expr: Any) -> str:
    return _ref_name(expr)


def _ref_name(expr: Any) -> str:
    if isinstance(expr, str):
        return expr
    if not isinstance(expr, Mapping):
        return str(expr)
    if "name" in expr:
        return str(expr.get("name") or "")
    if "field" in expr:
        return _ref_name(expr.get("field"))
    if "state" in expr:
        return _ref_name(expr.get("state"))
    return str(expr.get("signal") or "")


def z3_env(symbols: Mapping[str, Symbol]) -> dict[str, Any]:
    import z3

    env: dict[str, Any] = {}
    for name, symbol in symbols.items():
        typ = symbol.type
        if typ.kind == "bool":
            env[name] = z3.Bool(name)
        elif typ.kind in {"int", "uint"}:
            env[name] = z3.Int(name)
        elif typ.kind == "bitvector":
            env[name] = z3.BitVec(name, typ.width or 32)
        else:
            env[name] = z3.String(name)
    return env


def z3_constraints(symbols: Mapping[str, Symbol], env: Mapping[str, Any]) -> list[Any]:
    import z3

    constraints = []
    for name, symbol in symbols.items():
        var = env[name]
        typ = symbol.type
        if typ.kind == "enum" and typ.choices:
            constraints.append(z3.Or(*(var == str(choice) for choice in typ.choices)))
    return constraints


def z3_condition(expr: Any, env: Mapping[str, Any], symbols: Mapping[str, Symbol], externs: Mapping[str, Mapping[str, Any]]) -> Any:
    import z3

    if expr is None:
        return z3.BoolVal(True)
    value = z3_expr(expr, env, symbols, externs)
    return value if hasattr(value, "sort") and value.sort().kind() == z3.Z3_BOOL_SORT else value == True  # noqa: E712


def z3_expr(expr: Any, env: Mapping[str, Any], symbols: Mapping[str, Symbol], externs: Mapping[str, Mapping[str, Any]]) -> Any:
    import z3

    if isinstance(expr, bool):
        return z3.BoolVal(expr)
    if isinstance(expr, int):
        return z3.IntVal(expr)
    if isinstance(expr, bytes):
        return z3.StringVal(expr.hex())
    if isinstance(expr, str):
        return z3.StringVal(expr)
    if not isinstance(expr, Mapping):
        return z3.StringVal(str(expr))

    node = str(expr.get("node") or expr.get("kind") or "")
    if node in {"field_ref", "signal_ref", "state_ref", "field", "signal", "state"} or "field" in expr or "state" in expr:
        return env[_ref_name(expr)]

    if node == "literal" or "literal" in expr:
        value = expr.get("value") if node == "literal" else expr.get("literal")
        width = expr.get("width")
        if isinstance(value, bool):
            return z3.BoolVal(value)
        if isinstance(value, int):
            return z3.BitVecVal(value, width) if isinstance(width, int) and width > 0 else z3.IntVal(value)
        return z3.StringVal(str(value))

    if node == "compare" or "eq" in expr or "compare" in expr:
        if "eq" in expr:
            left_expr, right_expr = expr["eq"]
            op = "eq"
        else:
            spec = expr.get("compare", expr)
            left_expr, right_expr = spec.get("left"), spec.get("right")
            op = str(spec.get("op") or "eq")
        left = z3_expr(left_expr, env, symbols, externs)
        right = z3_expr(right_expr, env, symbols, externs)
        if op in {"==", "eq", "matches"}:
            return left == right
        if op in {"!=", "ne"}:
            return left != right
        if op in {"<", "lt"}:
            return left < right
        if op in {"<=", "le"}:
            return left <= right
        if op in {">", "gt"}:
            return left > right
        if op in {">=", "ge"}:
            return left >= right

    if node == "unary_op" or "not" in expr:
        op = str(expr.get("op") or ("not" if "not" in expr else ""))
        value = z3_expr(expr.get("operand", expr.get("not")), env, symbols, externs)
        if op in BOOL_UNARY_OPS:
            return z3.Not(value)
        if op in BIT_UNARY_OPS:
            return -value if op == "neg" else ~value

    if "and" in expr:
        return z3.And(*(z3_expr(item, env, symbols, externs) for item in expr["and"]))
    if "or" in expr:
        return z3.Or(*(z3_expr(item, env, symbols, externs) for item in expr["or"]))

    if node == "binary_op" or "binary" in expr:
        spec = expr.get("binary", expr)
        left = z3_expr(spec.get("left"), env, symbols, externs)
        right = z3_expr(spec.get("right"), env, symbols, externs)
        op = str(spec.get("op") or "")
        if op in {"and", "logical_and", "&&"}:
            return z3.And(left, right)
        if op in {"or", "logical_or", "||"}:
            return z3.Or(left, right)
        if op in {"xor", "bitwise_xor", "^"}:
            return left ^ right
        if op in {"add", "+"}:
            return left + right
        if op in {"sub", "-"}:
            return left - right
        if op in {"mul", "*"}:
            return left * right
        if op in {"bitwise_and", "&"}:
            return left & right
        if op in {"bitwise_or", "|"}:
            return left | right

    if node == "mux" or "if" in expr:
        spec = expr.get("if", expr)
        return z3.If(
            z3_condition(spec.get("condition", spec.get("cond")), env, symbols, externs),
            z3_expr(spec.get("when_true", spec.get("then")), env, symbols, externs),
            z3_expr(spec.get("when_false", spec.get("else")), env, symbols, externs),
        )

    if node == "concat" or "concat" in expr:
        parts = expr.get("parts", expr.get("concat", ()))
        values = [z3_expr(item, env, symbols, externs) for item in parts]
        coerced = []
        for value in values:
            if hasattr(value, "sort") and value.sort().kind() == z3.Z3_BOOL_SORT:
                coerced.append(z3.If(value, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1)))
            else:
                coerced.append(value)
        return z3.Concat(*coerced)

    if node == "slice" or "slice" in expr:
        spec = expr.get("slice", expr)
        return z3.Extract(int(spec.get("msb")), int(spec.get("lsb")), z3_expr(spec.get("value"), env, symbols, externs))

    if node == "cast":
        value = z3_expr(expr.get("value"), env, symbols, externs)
        width = expr.get("width")
        if isinstance(width, int) and width > 0:
            if hasattr(value, "sort") and value.sort().kind() == z3.Z3_BV_SORT:
                current = value.sort().size()
                if current == width:
                    return value
                if current < width:
                    return z3.ZeroExt(width - current, value)
                return z3.Extract(width - 1, 0, value)
            if hasattr(value, "sort") and value.sort().kind() == z3.Z3_INT_SORT:
                return z3.Int2BV(value, width)
        return value

    if "extern_call" in expr:
        extern_id = str(expr["extern_call"])
        extern = dict(externs.get(extern_id, {}))
        if extern.get("verification_policy") == "formal_model" and "formal_model" in extern:
            local_env = dict(env)
            for index, arg in enumerate(expr.get("args", ())):
                local_env[f"arg{index}"] = z3_expr(arg, env, symbols, externs)
            return z3_expr(extern["formal_model"], local_env, symbols, externs)
        raise ValueError(f"extern {extern_id!r} has no SMT formal_model")

    if node == "constraint":
        return z3_expr(expr.get("expr"), env, symbols, externs)
    if node == "implication":
        return z3.Implies(
            z3_condition(expr.get("antecedent"), env, symbols, externs),
            z3_condition(expr.get("consequent"), env, symbols, externs),
        )

    raise ValueError(f"unsupported SMT expression {expr!r}")
