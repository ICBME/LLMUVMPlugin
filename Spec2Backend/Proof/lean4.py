"""Lean4 proof backend for local Spec2Backend obligations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping

from Spec2Backend.Checks.expression import (
    ARITH_BINARY_OPS,
    BIT_BINARY_OPS,
    BIT_UNARY_OPS,
    BOOL_BINARY_OPS,
    BOOL_UNARY_OPS,
    ExpressionChecker,
)
from Spec2Backend.Checks.model import (
    BOOL,
    INT,
    ANY,
    CheckIssue,
    Symbol,
    TypeSpec,
    issue,
    merge_numeric,
    type_compatible,
)

from .model import ProofResult


FORBIDDEN_LEAN_TOKENS = re.compile(r"\b(sorry|admit|axiom|unsafe)\b")
LEAN_RESERVED = {
    "as",
    "axiom",
    "by",
    "class",
    "def",
    "do",
    "else",
    "end",
    "false",
    "forall",
    "fun",
    "have",
    "if",
    "import",
    "in",
    "inductive",
    "instance",
    "let",
    "match",
    "namespace",
    "open",
    "opaque",
    "rec",
    "section",
    "structure",
    "theorem",
    "then",
    "true",
    "universe",
    "unsafe",
    "where",
}


@dataclass(frozen=True)
class LeanTerm:
    text: str
    type: TypeSpec


class ProofBuildError(ValueError):
    """Raised when an obligation is outside the Lean v1 supported subset."""


class Lean4ProofBackend:
    name = "lean4"

    def __init__(
        self,
        *,
        lean_bin: str | os.PathLike[str] | None = None,
        timeout_s: float = 10.0,
        allowed_axioms: tuple[str, ...] = ("Classical.choice", "Quot.sound", "propext"),
    ):
        self.lean_bin = str(lean_bin) if lean_bin is not None else None
        self.timeout_s = float(timeout_s)
        self.allowed_axioms = tuple(str(item) for item in allowed_axioms)

    def prove_context(self, context: Any) -> ProofResult:
        command = discover_lean(self.lean_bin)
        metadata: dict[str, Any] = {
            "backend": self.name,
            "scope": "equivalence",
            "allowed_axioms": list(self.allowed_axioms),
            "obligation_count": len(tuple(getattr(context, "equivalences", ()))),
            "proved_obligations": [],
            "theorems": [],
        }
        if command is None:
            return ProofResult(
                "failed",
                issues=(issue("proof", "$", "Lean4 executable was not found", code="lean_missing"),),
                metadata=metadata,
            )
        metadata["lean_bin"] = command
        metadata["lean_version"] = lean_version(command, timeout_s=self.timeout_s)

        issues: list[CheckIssue] = []
        proved_rules: list[str] = []
        externs = {extern.extern_id: extern.spec for extern in getattr(context, "externs", ())}
        for index, equivalence in enumerate(getattr(context, "equivalences", ())):
            rule_id = str(equivalence.rule_id or f"equivalence_{index + 1}")
            theorem_name = lean_identifier(f"spec2backend_{rule_id}_{index + 1}")
            path = str(equivalence.path or "$")
            if getattr(equivalence, "trusted_extern_id", None):
                issues.append(
                    issue(
                        "proof",
                        path,
                        f"trusted extern {equivalence.trusted_extern_id!r} cannot be kernel-proved by Lean4",
                        rule_id=rule_id,
                        extern_id=str(equivalence.trusted_extern_id),
                        code="trusted_extern_not_proved",
                    )
                )
                continue
            try:
                source = LeanObligationBuilder(
                    getattr(context, "symbols", {}),
                    externs,
                ).build_equivalence(
                    equivalence.left,
                    equivalence.right,
                    condition=equivalence.condition,
                    theorem_name=theorem_name,
                )
            except ProofBuildError as exc:
                issues.append(issue("proof", path, str(exc), rule_id=rule_id, code="unsupported_obligation"))
                continue

            theorem_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            result = run_lean_source(
                source,
                command=command,
                timeout_s=self.timeout_s,
                theorem_name=theorem_name,
                allowed_axioms=self.allowed_axioms,
            )
            theorem_metadata = {
                "obligation_id": rule_id,
                "theorem": theorem_name,
                "sha256": theorem_hash,
                "path": path,
                "axioms": result["axioms"],
            }
            metadata["theorems"].append(theorem_metadata)
            if result["ok"]:
                metadata["proved_obligations"].append(rule_id)
                proved_rules.append(rule_id)
            else:
                issues.append(
                    issue(
                        "proof",
                        path,
                        str(result["message"]),
                        rule_id=rule_id,
                        code=str(result["code"]),
                    )
                )

        status = "failed" if any(item.blocking for item in issues) else "passed"
        return ProofResult(status, proved_rules=tuple(dict.fromkeys(proved_rules)), issues=tuple(issues), metadata=metadata)


class LeanObligationBuilder:
    def __init__(self, symbols: Mapping[str, Symbol], externs: Mapping[str, Mapping[str, Any]]):
        self.symbols = symbols
        self.externs = externs
        self.checker = ExpressionChecker(symbols, externs)
        self.name_map = lean_name_map(symbols)
        self.used_symbols: set[str] = set()
        self.uses_bitvector = False

    def build_equivalence(self, left: Any, right: Any, *, condition: Any | None, theorem_name: str) -> str:
        left_type, _ = self.checker.infer(left, "left")
        right_type, _ = self.checker.infer(right, "right")
        expected = common_expected_type(left_type, right_type)
        if expected.is_any:
            raise ProofBuildError("equivalence operands have unresolved any type")
        left_term = self.translate(left, expected=expected)
        right_term = self.translate(right, expected=expected)
        if not type_compatible(left_term.type, right_term.type):
            raise ProofBuildError(
                f"equivalence operands have incompatible Lean types {left_term.type.to_json()} and {right_term.type.to_json()}"
            )
        if left_term.type.kind not in {"bool", "int", "uint", "bitvector"}:
            raise ProofBuildError(f"Lean4 proof does not support type {left_term.type.to_json()}")

        proposition = f"({left_term.text} = {right_term.text})"
        if condition is not None:
            cond = self.translate(condition, expected=BOOL)
            if not cond.type.is_bool:
                raise ProofBuildError("Lean4 proof condition must be bool")
            proposition = f"({cond.text} = true) -> {proposition}"

        declarations = self.declarations()
        tactic = self.tactic()
        source = "\n".join(
            [
                "import Std.Tactic.BVDecide",
                "set_option autoImplicit false",
                f"theorem {theorem_name}{declarations} : {proposition} := by",
                *[f"  {line}" for line in tactic],
                f"#print axioms {theorem_name}",
                "",
            ]
        )
        reject_forbidden_source(source)
        return source

    def declarations(self) -> str:
        items = []
        for name in sorted(self.used_symbols):
            symbol = self.symbols.get(name)
            if symbol is None:
                raise ProofBuildError(f"unknown symbol {name!r}")
            items.append(f"({self.name_map[name]} : {lean_type(symbol.type)})")
        return " " + " ".join(items) if items else ""

    def tactic(self) -> list[str]:
        if self.uses_bitvector:
            return ["bv_decide"]
        bool_vars = [
            self.name_map[name]
            for name in sorted(self.used_symbols)
            if self.symbols[name].type.kind == "bool"
        ]
        if bool_vars:
            return [" <;> ".join([f"cases {name}" for name in bool_vars] + ["simp"])]
        return ["simp"]

    def translate(self, expr: Any, *, expected: TypeSpec | None = None) -> LeanTerm:
        if isinstance(expr, bool):
            return LeanTerm("true" if expr else "false", BOOL)
        if isinstance(expr, int):
            typ = expected if expected is not None and expected.kind in {"int", "uint", "bitvector"} else INT
            return LeanTerm(lean_literal_int(expr, typ), typ)
        if isinstance(expr, bytes):
            raise ProofBuildError("Lean4 proof does not support bytes literals")
        if isinstance(expr, str):
            raise ProofBuildError("Lean4 proof does not support string literals")
        if not isinstance(expr, Mapping):
            raise ProofBuildError(f"Lean4 proof does not support expression {expr!r}")

        if is_ref(expr):
            name = ref_name(expr)
            symbol = self.symbols.get(name)
            if symbol is None:
                raise ProofBuildError(f"unknown symbol {name!r}")
            self.check_supported_type(symbol.type, f"symbol {name!r}")
            self.used_symbols.add(name)
            return LeanTerm(self.name_map[name], symbol.type)

        node = str(expr.get("node") or expr.get("kind") or "")
        if node == "literal" or "literal" in expr:
            value = expr.get("value") if node == "literal" else expr.get("literal")
            literal_type = type_from_literal(value, expr, expected)
            self.check_supported_type(literal_type, "literal")
            if isinstance(value, bool):
                return LeanTerm("true" if value else "false", BOOL)
            if isinstance(value, int):
                return LeanTerm(lean_literal_int(value, literal_type), literal_type)
            raise ProofBuildError("Lean4 proof supports only bool/int literals")

        if "extern_call" in expr:
            raise ProofBuildError(f"Lean4 proof does not support extern {expr.get('extern_call')!r}")
        if "operation" in expr:
            raise ProofBuildError(f"Lean4 proof does not support operation {expr.get('operation')!r}")

        if node == "cast":
            target = TypeSpec(str(expr.get("type") or "any"), width=expr.get("width") if isinstance(expr.get("width"), int) else None)
            if target.kind in {"int", "uint"}:
                target = TypeSpec(target.kind)
            self.check_supported_type(target, "cast target")
            value = self.translate(expr.get("value"), expected=target)
            if not type_compatible(value.type, target):
                raise ProofBuildError(f"Lean4 proof does not support cast from {value.type.to_json()} to {target.to_json()}")
            return LeanTerm(f"({value.text} : {lean_type(target)})", target)

        if node == "compare" or "eq" in expr or "compare" in expr:
            left_expr, right_expr, op = compare_parts(expr)
            left_type, _ = self.checker.infer(left_expr, "compare.left")
            right_type, _ = self.checker.infer(right_expr, "compare.right")
            operand_type = common_expected_type(left_type, right_type)
            if operand_type.is_any:
                raise ProofBuildError("compare operands have unresolved any type")
            left = self.translate(left_expr, expected=operand_type)
            right = self.translate(right_expr, expected=operand_type)
            return LeanTerm(lean_compare(left.text, right.text, op), BOOL)

        if node == "unary_op" or "not" in expr:
            op = str(expr.get("op") or ("not" if "not" in expr else ""))
            operand = self.translate(expr.get("operand", expr.get("not")))
            if op in BOOL_UNARY_OPS:
                if not operand.type.is_bool:
                    raise ProofBuildError("logical unary operand must be bool")
                return LeanTerm(f"(!{operand.text})", BOOL)
            if op in BIT_UNARY_OPS:
                if operand.type.kind != "bitvector":
                    raise ProofBuildError("bitwise unary operand must be bitvector")
                self.uses_bitvector = True
                if op == "neg":
                    return LeanTerm(f"(-{operand.text})", operand.type)
                return LeanTerm(f"(~~~{operand.text})", operand.type)
            raise ProofBuildError(f"Lean4 proof does not support unary op {op!r}")

        if "and" in expr:
            parts = [self.translate(item, expected=BOOL) for item in expr.get("and", ())]
            if any(not part.type.is_bool for part in parts):
                raise ProofBuildError("and operands must be bool")
            return LeanTerm(chain_bool(parts, "&&", "true"), BOOL)
        if "or" in expr:
            parts = [self.translate(item, expected=BOOL) for item in expr.get("or", ())]
            if any(not part.type.is_bool for part in parts):
                raise ProofBuildError("or operands must be bool")
            return LeanTerm(chain_bool(parts, "||", "false"), BOOL)

        if node == "binary_op" or "binary" in expr:
            spec = expr.get("binary", expr)
            op = str(spec.get("op") or "")
            if op in BOOL_BINARY_OPS:
                left = self.translate(spec.get("left"), expected=BOOL)
                right = self.translate(spec.get("right"), expected=BOOL)
                if not left.type.is_bool or not right.type.is_bool:
                    raise ProofBuildError("logical binary operands must be bool")
                return LeanTerm(bool_binary(left.text, right.text, op), BOOL)
            if op in ARITH_BINARY_OPS | BIT_BINARY_OPS:
                left_type, _ = self.checker.infer(spec.get("left"), "binary.left")
                right_type, _ = self.checker.infer(spec.get("right"), "binary.right")
                result_type = merge_numeric(left_type, right_type)
                if result_type.is_any:
                    raise ProofBuildError("numeric binary operands have unresolved type")
                left = self.translate(spec.get("left"), expected=result_type)
                right = self.translate(spec.get("right"), expected=result_type)
                return LeanTerm(numeric_binary(left, right, op), result_type)
            raise ProofBuildError(f"Lean4 proof does not support binary op {op!r}")

        if node == "mux" or "if" in expr:
            spec = expr.get("if", expr)
            cond = self.translate(spec.get("condition", spec.get("cond")), expected=BOOL)
            if not cond.type.is_bool:
                raise ProofBuildError("mux condition must be bool")
            true_type, _ = self.checker.infer(spec.get("when_true", spec.get("then")), "if.then")
            false_type, _ = self.checker.infer(spec.get("when_false", spec.get("else")), "if.else")
            result_type = common_expected_type(true_type, false_type)
            when_true = self.translate(spec.get("when_true", spec.get("then")), expected=result_type)
            when_false = self.translate(spec.get("when_false", spec.get("else")), expected=result_type)
            return LeanTerm(f"(if {cond.text} then {when_true.text} else {when_false.text})", result_type)

        if node in {"concat", "slice", "reduce"} or any(key in expr for key in ("concat", "slice")):
            raise ProofBuildError(f"Lean4 proof does not support {node or 'bitvector structural'} expressions in v1")

        raise ProofBuildError(f"Lean4 proof does not support expression {expr!r}")

    def check_supported_type(self, typ: TypeSpec, label: str) -> None:
        if typ.is_any:
            raise ProofBuildError(f"{label} has unresolved any type")
        if typ.kind == "bitvector":
            if typ.width is None:
                raise ProofBuildError(f"{label} bitvector type must define width")
            self.uses_bitvector = True
            return
        if typ.kind not in {"bool", "int", "uint"}:
            raise ProofBuildError(f"Lean4 proof does not support {label} type {typ.to_json()}")


def discover_lean(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_value = os.environ.get("LEAN_BIN")
    if env_value:
        candidates.append(env_value)
    path_value = shutil.which("lean")
    if path_value:
        candidates.append(path_value)
    candidates.append(str(Path.home() / ".elan" / "bin" / "lean"))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
    return None


def lean_version(command: str, *, timeout_s: float) -> str:
    try:
        completed = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {type(exc).__name__}: {exc}"
    return (completed.stdout or completed.stderr).strip()


def run_lean_source(
    source: str,
    *,
    command: str,
    timeout_s: float,
    theorem_name: str,
    allowed_axioms: tuple[str, ...],
) -> dict[str, Any]:
    try:
        reject_forbidden_source(source)
    except ValueError as exc:
        return {"ok": False, "code": "forbidden_proof_token", "message": str(exc), "axioms": []}
    try:
        completed = subprocess.run(
            [command, "--stdin", "--json"],
            input=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "lean_timeout", "message": "Lean4 proof timed out", "axioms": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "lean_error", "message": f"{type(exc).__name__}: {exc}", "axioms": []}

    messages = lean_json_messages(completed.stdout)
    errors = [message for message in messages if message.get("severity") == "error"]
    if completed.returncode != 0 or errors:
        text = "\n".join(str(message.get("data") or "") for message in errors).strip()
        if not text:
            text = (completed.stderr or completed.stdout or "Lean4 proof failed").strip()
        return {"ok": False, "code": "lean_rejected", "message": text, "axioms": []}

    axioms = axioms_for_theorem(messages, theorem_name)
    unapproved = sorted(
        axiom
        for axiom in set(axioms)
        if axiom not in set(allowed_axioms) and not is_allowed_native_bvdecide_axiom(axiom, theorem_name)
    )
    if unapproved:
        return {
            "ok": False,
            "code": "lean_axioms",
            "message": f"Lean theorem depends on unapproved axioms: {unapproved}",
            "axioms": axioms,
        }
    return {"ok": True, "code": "proved", "message": "proved", "axioms": axioms}


def is_allowed_native_bvdecide_axiom(axiom_name: str, theorem_name: str) -> bool:
    return axiom_name.startswith(f"{theorem_name}._native.bv_decide.ax_")


def lean_json_messages(output: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            messages.append(value)
    return messages


def axioms_for_theorem(messages: list[dict[str, Any]], theorem_name: str) -> list[str]:
    for message in messages:
        data = str(message.get("data") or "")
        if theorem_name not in data:
            continue
        if "does not depend on any axioms" in data:
            return []
        match = re.search(r"depends on axioms:\s*\[([^\]]*)\]", data)
        if match:
            raw = match.group(1).strip()
            return [item.strip() for item in raw.split(",") if item.strip()]
    return ["missing_axiom_report"]


def reject_forbidden_source(source: str) -> None:
    match = FORBIDDEN_LEAN_TOKENS.search(source)
    if match:
        raise ValueError(f"Lean proof source contains forbidden token {match.group(1)!r}")


def lean_identifier(name: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized or normalized[0].isdigit() or normalized in LEAN_RESERVED:
        normalized = f"v_{normalized}" if normalized else "v"
    return normalized


def lean_name_map(symbols: Mapping[str, Symbol]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for name in symbols:
        candidate = lean_identifier(name)
        if candidate in used:
            suffix = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:8]
            candidate = lean_identifier(f"{candidate}_{suffix}")
        while candidate in used:
            candidate = lean_identifier(f"{candidate}_v")
        result[name] = candidate
        used.add(candidate)
    return result


def lean_type(typ: TypeSpec) -> str:
    if typ.kind == "bool":
        return "Bool"
    if typ.kind in {"int", "uint"}:
        return "Int"
    if typ.kind == "bitvector" and typ.width is not None:
        return f"BitVec {typ.width}"
    raise ProofBuildError(f"unsupported Lean type {typ.to_json()}")


def lean_literal_int(value: int, typ: TypeSpec) -> str:
    if typ.kind == "bitvector":
        if typ.width is None:
            raise ProofBuildError("bitvector literal requires width")
        return f"({int(value)} : BitVec {typ.width})"
    return f"({int(value)} : Int)"


def type_from_literal(value: Any, expr: Mapping[str, Any], expected: TypeSpec | None) -> TypeSpec:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        width = expr.get("width")
        if isinstance(width, int) and width > 0:
            return TypeSpec("bitvector", width=width)
        if expected is not None and expected.kind in {"int", "uint", "bitvector"}:
            return expected
        return INT
    return ANY


def common_expected_type(left: TypeSpec, right: TypeSpec) -> TypeSpec:
    if left.is_any:
        return right
    if right.is_any:
        return left
    if type_compatible(left, right):
        if left.kind == "bitvector":
            return left if left.width is not None else right
        if right.kind == "bitvector":
            return right
        if left.kind == right.kind:
            return left
        if left.kind in {"int", "uint"} and right.kind in {"int", "uint"}:
            return INT
    return ANY


def is_ref(expr: Mapping[str, Any]) -> bool:
    node = str(expr.get("node") or expr.get("kind") or "")
    return node in {"field_ref", "signal_ref", "state_ref", "field", "signal", "state"} or any(
        key in expr for key in ("field", "signal", "state")
    )


def ref_name(expr: Mapping[str, Any]) -> str:
    if "name" in expr:
        return str(expr.get("name") or "")
    for key in ("field", "signal", "state"):
        if key in expr:
            value = expr.get(key)
            if isinstance(value, Mapping):
                return str(value.get("name") or value.get(key) or "")
            return str(value or "")
    return ""


def compare_parts(expr: Mapping[str, Any]) -> tuple[Any, Any, str]:
    if "eq" in expr:
        operands = expr.get("eq")
        if isinstance(operands, list | tuple) and len(operands) == 2:
            return operands[0], operands[1], "eq"
        raise ProofBuildError("eq compare requires two operands")
    spec = expr.get("compare", expr)
    if not isinstance(spec, Mapping):
        raise ProofBuildError("compare expression must be an object")
    return spec.get("left"), spec.get("right"), str(spec.get("op") or "eq")


def lean_compare(left: str, right: str, op: str) -> str:
    if op in {"==", "eq", "matches"}:
        return f"(decide ({left} = {right}))"
    if op in {"!=", "ne"}:
        return f"(decide ({left} ≠ {right}))"
    if op in {"<", "lt"}:
        return f"(decide ({left} < {right}))"
    if op in {"<=", "le"}:
        return f"(decide ({left} <= {right}))"
    if op in {">", "gt"}:
        return f"(decide ({left} > {right}))"
    if op in {">=", "ge"}:
        return f"(decide ({left} >= {right}))"
    raise ProofBuildError(f"Lean4 proof does not support compare op {op!r}")


def chain_bool(parts: list[LeanTerm], op: str, empty: str) -> str:
    if not parts:
        return empty
    result = parts[0].text
    for part in parts[1:]:
        result = f"({result} {op} {part.text})"
    return result


def bool_binary(left: str, right: str, op: str) -> str:
    if op in {"and", "logical_and", "&&"}:
        return f"({left} && {right})"
    if op in {"or", "logical_or", "||"}:
        return f"({left} || {right})"
    if op in {"xor", "bitwise_xor", "^"}:
        return f"(Bool.xor {left} {right})"
    raise ProofBuildError(f"Lean4 proof does not support bool op {op!r}")


def numeric_binary(left: LeanTerm, right: LeanTerm, op: str) -> str:
    if left.type.kind == "bitvector" or right.type.kind == "bitvector":
        if left.type.kind != "bitvector" or right.type.kind != "bitvector" or left.type.width != right.type.width:
            raise ProofBuildError("bitvector operands must have matching widths")
        if op in {"add", "+"}:
            return f"({left.text} + {right.text})"
        if op in {"sub", "-"}:
            return f"({left.text} - {right.text})"
        if op in {"mul", "*"}:
            return f"({left.text} * {right.text})"
        if op in {"bitwise_and", "&"}:
            return f"({left.text} &&& {right.text})"
        if op in {"bitwise_or", "|"}:
            return f"({left.text} ||| {right.text})"
        if op in {"xor", "bitwise_xor", "^"}:
            return f"({left.text} ^^^ {right.text})"
        raise ProofBuildError(f"Lean4 proof does not support bitvector op {op!r}")
    if op in {"add", "+"}:
        return f"({left.text} + {right.text})"
    if op in {"sub", "-"}:
        return f"({left.text} - {right.text})"
    if op in {"mul", "*"}:
        return f"({left.text} * {right.text})"
    raise ProofBuildError(f"Lean4 proof does not support integer op {op!r}")
