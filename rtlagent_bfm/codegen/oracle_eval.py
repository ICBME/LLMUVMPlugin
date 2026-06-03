"""Runtime evaluator for the narrow OracleIR DSL."""

from __future__ import annotations

import binascii
import hashlib
from typing import Any
import zlib


class OracleEvaluationError(ValueError):
    """Raised when an OracleIR cannot produce an expected value for a case."""


def evaluate_oracle_ir(oracle_ir: dict[str, Any], case_data: dict[str, Any]) -> str:
    """Evaluate the first matching OracleIR prediction rule for ``case_data``."""

    rules = oracle_ir.get("rules", [])
    if not isinstance(rules, list) or not rules:
        raise OracleEvaluationError("OracleIR does not contain prediction rules")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        condition = rule.get("when", True)
        if evaluate_condition(condition, case_data):
            if "expected" not in rule:
                raise OracleEvaluationError(f"rules[{index}] is missing expected expression")
            return format_expected(evaluate_expr(rule["expected"], case_data))
    raise OracleEvaluationError("no OracleIR rule matched case data")


def evaluate_condition(expr: Any, case_data: dict[str, Any]) -> bool:
    if isinstance(expr, bool):
        return expr
    if not isinstance(expr, dict):
        return bool(evaluate_expr(expr, case_data))
    if "eq" in expr:
        pair = expr["eq"]
        if not isinstance(pair, list) or len(pair) != 2:
            raise OracleEvaluationError("eq condition must contain two operands")
        return evaluate_expr(pair[0], case_data) == evaluate_expr(pair[1], case_data)
    if "and" in expr:
        values = expr["and"]
        if not isinstance(values, list):
            raise OracleEvaluationError("and condition must contain a list")
        return all(evaluate_condition(item, case_data) for item in values)
    if "or" in expr:
        values = expr["or"]
        if not isinstance(values, list):
            raise OracleEvaluationError("or condition must contain a list")
        return any(evaluate_condition(item, case_data) for item in values)
    if "not" in expr:
        return not evaluate_condition(expr["not"], case_data)
    return bool(evaluate_expr(expr, case_data))


def evaluate_expr(expr: Any, case_data: dict[str, Any]) -> Any:
    if isinstance(expr, str | int | float | bool) or expr is None:
        return expr
    if isinstance(expr, list):
        return [evaluate_expr(item, case_data) for item in expr]
    if not isinstance(expr, dict):
        raise OracleEvaluationError(f"unsupported expression type {type(expr).__name__}")

    if "literal" in expr:
        return expr["literal"]
    if "field" in expr:
        field_name = str(expr["field"])
        if field_name not in case_data:
            raise OracleEvaluationError(f"case data is missing field {field_name!r}")
        return case_data[field_name]
    if "bytes_from_hex" in expr:
        value = evaluate_expr(expr["bytes_from_hex"], case_data)
        try:
            return bytes.fromhex(str(value))
        except ValueError as exc:
            raise OracleEvaluationError("bytes_from_hex operand is not valid hex") from exc
    if "call" in expr:
        return evaluate_call(expr, case_data)
    if "concat" in expr:
        values = evaluate_expr(expr["concat"], case_data)
        if not isinstance(values, list):
            raise OracleEvaluationError("concat operand must evaluate to a list")
        return concat_values(values)
    if "slice" in expr:
        return evaluate_slice(expr["slice"], case_data)
    if "lower_hex" in expr:
        return str(evaluate_expr(expr["lower_hex"], case_data)).lower()
    if any(key in expr for key in ("and", "eq", "not", "or")):
        return evaluate_condition(expr, case_data)
    raise OracleEvaluationError(f"unknown expression operator(s): {sorted(expr)}")


def evaluate_call(expr: dict[str, Any], case_data: dict[str, Any]) -> Any:
    name = str(expr.get("call", ""))
    args = [evaluate_expr(arg, case_data) for arg in expr.get("args", [])]
    result_format = expr.get("format")
    result = call_allowlisted_function(name, args)
    return format_call_result(result, result_format)


def call_allowlisted_function(name: str, args: list[Any]) -> Any:
    if name.startswith("hashlib."):
        if len(args) != 1 or not isinstance(args[0], bytes):
            raise OracleEvaluationError(f"{name} expects one bytes argument")
        algorithm = name.split(".", 1)[1]
        try:
            return getattr(hashlib, algorithm)(args[0])
        except AttributeError as exc:
            raise OracleEvaluationError(f"unsupported hashlib algorithm {algorithm!r}") from exc
    if name == "binascii.crc32":
        if len(args) != 1 or not isinstance(args[0], bytes):
            raise OracleEvaluationError("binascii.crc32 expects one bytes argument")
        return binascii.crc32(args[0])
    if name == "zlib.crc32":
        if len(args) != 1 or not isinstance(args[0], bytes):
            raise OracleEvaluationError("zlib.crc32 expects one bytes argument")
        return zlib.crc32(args[0])
    raise OracleEvaluationError(f"call {name!r} is not supported by evaluator")


def format_call_result(result: Any, result_format: Any) -> Any:
    if result_format is None:
        return result
    if result_format == "hexdigest":
        if not callable(getattr(result, "hexdigest", None)):
            raise OracleEvaluationError("hexdigest format requires a hash object")
        return result.hexdigest()
    if result_format == "digest":
        if not callable(getattr(result, "digest", None)):
            raise OracleEvaluationError("digest format requires a hash object")
        return result.digest()
    if result_format == "hex":
        if isinstance(result, bytes):
            return result.hex()
        if isinstance(result, int):
            return f"{result:x}"
        if callable(getattr(result, "hex", None)):
            return result.hex()
        raise OracleEvaluationError("hex format requires bytes, int, or object.hex()")
    if result_format == "int":
        return int(result)
    if result_format == "str":
        return str(result)
    raise OracleEvaluationError(f"unsupported call result format {result_format!r}")


def evaluate_slice(value: Any, case_data: dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        raise OracleEvaluationError("slice expression must be an object")
    item = evaluate_expr(value.get("value"), case_data)
    start = value.get("start")
    end = value.get("end")
    if start is not None and not isinstance(start, int):
        raise OracleEvaluationError("slice.start must be an integer")
    if end is not None and not isinstance(end, int):
        raise OracleEvaluationError("slice.end must be an integer")
    return item[start:end]


def concat_values(values: list[Any]) -> Any:
    if all(isinstance(value, bytes) for value in values):
        return b"".join(values)
    return "".join(format_expected(value) for value in values)


def format_expected(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)
