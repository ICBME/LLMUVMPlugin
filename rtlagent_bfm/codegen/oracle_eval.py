"""Runtime evaluator for the narrow OracleIR DSL."""

from __future__ import annotations

import binascii
from dataclasses import dataclass
import hashlib
from typing import Any
import zlib


class OracleEvaluationError(ValueError):
    """Raised when an OracleIR cannot produce an expected value for a case."""


@dataclass(frozen=True)
class OracleGoldenIssue:
    line_no: int
    reason: str
    actual: str | None = None
    expected: str | None = None

    @property
    def path(self) -> str:
        return f"golden_cases[line={self.line_no}]"

    @property
    def message(self) -> str:
        if self.actual is None and self.expected is None:
            return self.reason
        return f"{self.reason}: actual={self.actual!r} expected={self.expected!r}"


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


def validate_oracle_golden_cases(
    oracle_ir: dict[str, Any],
    golden_cases: Any,
) -> None:
    issues = collect_oracle_golden_issues(oracle_ir, golden_cases)
    if issues:
        formatted = "\n".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise OracleEvaluationError(f"OracleIR golden validation failed:\n{formatted}")


def collect_oracle_golden_issues(
    oracle_ir: dict[str, Any],
    golden_cases: Any,
) -> list[OracleGoldenIssue]:
    issues: list[OracleGoldenIssue] = []
    for index, golden_case in enumerate(golden_cases, start=1):
        line_no = golden_line_no(golden_case, index)
        try:
            data = golden_data(golden_case)
            expected = format_expected(golden_expected(golden_case))
            actual = evaluate_oracle_ir(oracle_ir, data)
        except Exception as exc:  # noqa: BLE001 - preserve evaluator context in feedback
            issues.append(OracleGoldenIssue(line_no=line_no, reason=str(exc)))
            continue
        if not compare_expected(actual, expected, oracle_ir.get("compare", {})):
            issues.append(
                OracleGoldenIssue(
                    line_no=line_no,
                    reason="expected value mismatch",
                    actual=actual,
                    expected=expected,
                )
            )
    return issues


def compare_expected(actual: str, expected: str, compare_policy: dict[str, Any]) -> bool:
    kind = str(compare_policy.get("kind", "exact"))
    actual_text = normalize_compare_text(actual, compare_policy)
    expected_text = normalize_compare_text(expected, compare_policy)
    if kind == "exact":
        return actual_text == expected_text
    if kind == "prefix":
        length = int(compare_policy.get("length", len(expected_text)))
        return actual_text[:length] == expected_text[:length]
    if kind == "masked_hex":
        mask = int(str(compare_policy["mask"]).removeprefix("0x"), 16)
        return (int(actual_text, 16) & mask) == (int(expected_text, 16) & mask)
    if kind == "numeric_tolerance":
        tolerance = float(compare_policy.get("tolerance", 0))
        return abs(float(actual_text) - float(expected_text)) <= tolerance
    raise OracleEvaluationError(f"unsupported compare kind {kind!r}")


def normalize_compare_text(value: Any, compare_policy: dict[str, Any]) -> str:
    text = format_expected(value)
    normalizers = compare_policy.get("normalize", [])
    if not isinstance(normalizers, list):
        normalizers = []
    for normalizer in normalizers:
        if normalizer == "strip_0x":
            text = text.removeprefix("0x").removeprefix("0X")
        elif normalizer == "lower_hex":
            text = text.lower()
        elif normalizer == "upper_hex":
            text = text.upper()
    return text


def golden_data(golden_case: Any) -> dict[str, Any]:
    if hasattr(golden_case, "data"):
        data = golden_case.data
    elif isinstance(golden_case, dict):
        data = golden_case.get("data", golden_case.get("case"))
    else:
        data = None
    if not isinstance(data, dict):
        raise OracleEvaluationError("golden case must define data or case object")
    return data


def golden_expected(golden_case: Any) -> Any:
    if hasattr(golden_case, "expected"):
        return golden_case.expected
    if isinstance(golden_case, dict) and "expected" in golden_case:
        return golden_case["expected"]
    raise OracleEvaluationError("golden case must define expected")


def golden_line_no(golden_case: Any, default: int) -> int:
    if hasattr(golden_case, "line_no"):
        return int(golden_case.line_no)
    if isinstance(golden_case, dict):
        return int(golden_case.get("line_no", default))
    return default
