"""Interpreter for the RefModelIR DSL."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .extern import ExternRegistry
from .schema import RefModelDSLError, json_safe, normalize_ref_model_ir, read_json


class RefModelInterpreter:
    """Small deterministic interpreter used by generated UVM ref model wrappers."""

    def __init__(self, ref_model_ir: Mapping[str, Any], *, base_dir: str | Path = "."):
        self.ir = normalize_ref_model_ir(ref_model_ir)
        self.base_dir = Path(base_dir)
        self.externs = ExternRegistry(self.ir.get("externs", {}), base_dir=self.base_dir)

    @classmethod
    def from_path(cls, path: str | Path) -> "RefModelInterpreter":
        ir_path = Path(path)
        return cls(read_json(ir_path), base_dir=ir_path.parent)

    def initial_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for name, spec in dict(self.ir.get("state", {})).items():
            value = None
            if isinstance(spec, Mapping):
                value = spec.get("initial", spec.get("default"))
            state[str(name)] = json_safe(value)
        env = {"inputs": {}, "state": state}
        for rule in self.ir.get("init_rules", ()):
            if not isinstance(rule, Mapping):
                raise RefModelDSLError("RefModelIR init_rules must be mappings")
            when = rule.get("when")
            if when is not None and not bool(self.eval_expr(when, env)):
                continue
            updates = rule.get("state_updates", rule.get("assign", {}))
            if not isinstance(updates, Mapping):
                raise RefModelDSLError("RefModelIR init rule updates must be a mapping")
            state.update({str(name): self.eval_expr(expr, env) for name, expr in updates.items()})
        return json_safe(state)

    def eval(self, data: Mapping[str, Any], *, state: Mapping[str, Any] | None = None) -> Any:
        outputs = self.eval_outputs(data, state=state)
        if "expected" in outputs:
            return json_safe(outputs["expected"])
        if len(outputs) == 1:
            return json_safe(next(iter(outputs.values())))
        return json_safe(outputs)

    def eval_outputs(
        self,
        data: Mapping[str, Any],
        *,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        outputs, _next_state = self.step(data, state=state)
        return outputs

    def step(
        self,
        data: Mapping[str, Any],
        *,
        state: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_state = self.initial_state() if state is None else dict(state)
        env = {"inputs": dict(data), "state": current_state}
        outputs: dict[str, Any] = {}
        for rule in self.ir.get("rules", ()):
            if not isinstance(rule, Mapping):
                raise RefModelDSLError("RefModelIR rules must be mappings")
            when = rule.get("when")
            if when is not None and not bool(self.eval_expr(when, env)):
                continue
            assign = rule.get("assign", {})
            if not isinstance(assign, Mapping):
                raise RefModelDSLError("RefModelIR rule assign must be a mapping")
            for name, expr in assign.items():
                outputs[str(name)] = self.eval_expr(expr, env)
        for rule in self.ir.get("step_rules", ()):
            if not isinstance(rule, Mapping):
                raise RefModelDSLError("RefModelIR step_rules must be mappings")
            when = rule.get("when")
            if when is not None and not bool(self.eval_expr(when, env)):
                continue
            for name, expr in dict(rule.get("assign", {})).items():
                outputs[str(name)] = self.eval_expr(expr, env)
            updates = {str(name): self.eval_expr(expr, env) for name, expr in dict(rule.get("state_updates", {})).items()}
            env["state"].update(updates)
        return json_safe(outputs), json_safe(env["state"])

    def eval_expr(self, expr: Any, env: Mapping[str, Any]) -> Any:
        if not isinstance(expr, Mapping):
            return expr
        if "literal" in expr:
            return expr["literal"]
        if expr.get("kind") == "literal":
            return expr.get("value")
        if "field" in expr or expr.get("kind") == "field":
            name = _field_name(expr.get("field", expr))
            inputs = env.get("inputs", {})
            if name not in inputs:
                raise RefModelDSLError(f"input field {name!r} is not present")
            return inputs[name]
        if expr.get("kind") == "state" or "state" in expr:
            name = _field_name(expr.get("state", expr))
            state = env.get("state", {})
            if name not in state:
                raise RefModelDSLError(f"state field {name!r} is not present")
            return state[name]
        if "decode_hex" in expr:
            return bytes.fromhex(str(self.eval_expr(expr["decode_hex"], env)))
        if "encode_hex" in expr:
            value = self.eval_expr(expr["encode_hex"], env)
            if not isinstance(value, bytes):
                raise RefModelDSLError("encode_hex expects bytes")
            return value.hex()
        if "extern_call" in expr:
            args = [self.eval_expr(item, env) for item in expr.get("args", ())]
            return self.externs.call(str(expr["extern_call"]), args, result=expr.get("result"))
        if "if" in expr:
            spec = expr["if"]
            if not isinstance(spec, Mapping):
                raise RefModelDSLError("if expression must be a mapping")
            branch = spec.get("then") if self.eval_expr(spec.get("cond"), env) else spec.get("else")
            return self.eval_expr(branch, env)
        if "match" in expr:
            return self._eval_match(expr["match"], env)
        if "eq" in expr:
            left, right = _pair(expr["eq"], "eq")
            return self.eval_expr(left, env) == self.eval_expr(right, env)
        if expr.get("kind") == "compare" or "compare" in expr:
            return self._eval_compare(expr.get("compare", expr), env)
        if "not" in expr or expr.get("kind") == "unary_op":
            op = str(expr.get("op") or ("not" if "not" in expr else ""))
            value = self.eval_expr(expr.get("not", expr.get("operand")), env)
            if op in {"not", "!"}:
                return not bool(value)
            if op in {"bit_not", "~"}:
                return ~int(value)
            raise RefModelDSLError(f"unsupported unary op {op!r}")
        if "and" in expr:
            return all(bool(self.eval_expr(item, env)) for item in expr["and"])
        if "or" in expr:
            return any(bool(self.eval_expr(item, env)) for item in expr["or"])
        if expr.get("kind") == "binary_op" or "binary" in expr:
            return self._eval_binary(expr.get("binary", expr), env)
        if "concat" in expr:
            part_exprs = _list_value(expr["concat"])
            parts = [self.eval_expr(item, env) for item in part_exprs]
            if all(isinstance(part, str) for part in parts):
                return "".join(parts)
            result = 0
            for part_expr, part in zip(part_exprs, parts):
                result = (result << _expr_bit_width(part_expr, part)) | int(part)
            return result
        if "slice" in expr or expr.get("kind") == "slice":
            spec = expr.get("slice", expr)
            value = int(self.eval_expr(spec.get("value"), env))
            msb = int(spec.get("msb"))
            lsb = int(spec.get("lsb"))
            mask = (1 << (msb - lsb + 1)) - 1
            return (value >> lsb) & mask
        if "cast" in expr or expr.get("kind") == "cast":
            spec = expr.get("cast", expr)
            value = self.eval_expr(spec.get("value"), env)
            typ = str(spec.get("type") or spec.get("to") or "")
            if typ in {"int", "uint", "bitvector"}:
                return int(value)
            if typ in {"bool"}:
                return bool(value)
            if typ in {"str", "string", "hex_string"}:
                return str(value)
            return value
        raise RefModelDSLError(f"unsupported DSL expression: {expr}")

    def _eval_match(self, spec: Any, env: Mapping[str, Any]) -> Any:
        if not isinstance(spec, Mapping):
            raise RefModelDSLError("match expression must be a mapping")
        value = self.eval_expr(spec.get("value"), env)
        for case in spec.get("cases", ()):
            if not isinstance(case, Mapping):
                continue
            if value == self.eval_expr(case.get("when"), env):
                return self.eval_expr(case.get("then"), env)
        if "default" in spec:
            return self.eval_expr(spec["default"], env)
        raise RefModelDSLError(f"match expression has no case for {value!r}")

    def _eval_compare(self, spec: Any, env: Mapping[str, Any]) -> bool:
        if isinstance(spec, list | tuple):
            op, left, right = spec
        elif isinstance(spec, Mapping):
            op, left, right = spec.get("op"), spec.get("left"), spec.get("right")
        else:
            raise RefModelDSLError("compare expression must be a mapping or tuple")
        left_value = self.eval_expr(left, env)
        right_value = self.eval_expr(right, env)
        if op in {"==", "eq"}:
            return left_value == right_value
        if op in {"!=", "ne"}:
            return left_value != right_value
        if op == "<":
            return left_value < right_value
        if op == "<=":
            return left_value <= right_value
        if op == ">":
            return left_value > right_value
        if op == ">=":
            return left_value >= right_value
        raise RefModelDSLError(f"unsupported compare op {op!r}")

    def _eval_binary(self, spec: Any, env: Mapping[str, Any]) -> Any:
        if isinstance(spec, Mapping):
            op, left, right = spec.get("op"), spec.get("left"), spec.get("right")
        else:
            raise RefModelDSLError("binary expression must be a mapping")
        left_value = self.eval_expr(left, env)
        right_value = self.eval_expr(right, env)
        if op in {"and", "&&"}:
            return bool(left_value) and bool(right_value)
        if op in {"or", "||"}:
            return bool(left_value) or bool(right_value)
        if op in {"xor", "^"}:
            return int(left_value) ^ int(right_value)
        if op in {"add", "+"}:
            return int(left_value) + int(right_value)
        if op in {"sub", "-"}:
            return int(left_value) - int(right_value)
        if op in {"mul", "*"}:
            return int(left_value) * int(right_value)
        raise RefModelDSLError(f"unsupported binary op {op!r}")


def _field_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("field") or value.get("state") or "")
    return str(value)


def _pair(value: Any, spec: str) -> tuple[Any, Any]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise RefModelDSLError(f"{spec} expression must contain exactly two operands")
    return value[0], value[1]


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        value = value.get("parts", ())
    if not isinstance(value, list | tuple):
        raise RefModelDSLError("expected expression list")
    return list(value)


def _bit_length(value: Any) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return max(1, value.bit_length())
    return 8 * len(str(value))


def _expr_bit_width(expr: Any, value: Any) -> int:
    if isinstance(expr, Mapping):
        if expr.get("width") is not None:
            return int(expr["width"])
        spec = expr.get("slice", expr) if "slice" in expr or expr.get("kind") == "slice" else None
        if isinstance(spec, Mapping) and spec.get("msb") is not None and spec.get("lsb") is not None:
            return int(spec["msb"]) - int(spec["lsb"]) + 1
    return _bit_length(value)


__all__ = ["RefModelInterpreter"]
