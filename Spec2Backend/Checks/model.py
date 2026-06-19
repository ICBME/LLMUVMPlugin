"""Shared check report and type models for Spec2Backend IRs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class TypeSpec:
    kind: str
    width: int | None = None
    choices: tuple[Any, ...] = ()
    format: str | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind}
        if self.width is not None:
            value["width"] = self.width
        if self.choices:
            value["choices"] = list(self.choices)
        if self.format is not None:
            value["format"] = self.format
        return value

    @property
    def is_bool(self) -> bool:
        return self.kind == "bool"

    @property
    def is_numeric(self) -> bool:
        return self.kind in {"int", "uint", "bitvector"}

    @property
    def is_any(self) -> bool:
        return self.kind == "any"


BOOL = TypeSpec("bool")
INT = TypeSpec("int")
UINT = TypeSpec("uint")
STRING = TypeSpec("string")
ANY = TypeSpec("any")


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    type: TypeSpec = ANY
    direction: str | None = None
    roles: tuple[str, ...] = ()
    source: str | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "type": self.type.to_json(),
            "roles": list(self.roles),
        }
        if self.direction is not None:
            value["direction"] = self.direction
        if self.source is not None:
            value["source"] = self.source
        return value


@dataclass(frozen=True)
class CheckIssue:
    stage: str
    message: str
    severity: str = "error"
    path: str | None = None
    blocking: bool = True
    rule_id: str | None = None
    extern_id: str | None = None
    code: str | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stage": self.stage,
            "severity": self.severity,
            "message": self.message,
            "blocking": self.blocking,
        }
        if self.path is not None:
            value["path"] = self.path
        if self.rule_id is not None:
            value["rule_id"] = self.rule_id
        if self.extern_id is not None:
            value["extern_id"] = self.extern_id
        if self.code is not None:
            value["code"] = self.code
        return value


@dataclass(frozen=True)
class CheckReport:
    status: str
    verification_level: str = "schema_verified"
    proved_rules: tuple[str, ...] = ()
    trusted_standard_externs: tuple[str, ...] = ()
    tested_externs: tuple[str, ...] = ()
    issues: tuple[CheckIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed" and not self.blocked_issues

    @property
    def blocked_issues(self) -> tuple[CheckIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verification_level": self.verification_level,
            "proved_rules": list(self.proved_rules),
            "trusted_standard_externs": list(self.trusted_standard_externs),
            "tested_externs": list(self.tested_externs),
            "blocked_issues": [issue.to_json() for issue in self.blocked_issues],
            "issues": [issue.to_json() for issue in self.issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class AssignmentCheck:
    path: str
    target: Any
    value: Any
    condition: Any | None = None
    rule_id: str | None = None


@dataclass(frozen=True)
class PredicateCheck:
    path: str
    expr: Any
    rule_id: str | None = None


@dataclass(frozen=True)
class ExpressionCheck:
    path: str
    expr: Any
    rule_id: str | None = None


@dataclass(frozen=True)
class TotalityCheck:
    path: str
    output: str
    rules: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class EquivalenceCheck:
    path: str
    left: Any
    right: Any
    condition: Any | None = None
    rule_id: str | None = None
    trusted_extern_id: str | None = None


@dataclass(frozen=True)
class FormalObligation:
    obligation_id: str
    kind: str
    expr: Any
    path: str
    rule_id: str | None = None


@dataclass(frozen=True)
class ExternCheck:
    extern_id: str
    spec: Mapping[str, Any]
    path: str


@dataclass(frozen=True)
class CheckContext:
    target: str
    symbols: Mapping[str, Symbol]
    expressions: tuple[ExpressionCheck, ...] = ()
    assignments: tuple[AssignmentCheck, ...] = ()
    predicates: tuple[PredicateCheck, ...] = ()
    totality: tuple[TotalityCheck, ...] = ()
    equivalences: tuple[EquivalenceCheck, ...] = ()
    externs: tuple[ExternCheck, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


def issue(
    stage: str,
    path: str | None,
    message: str,
    *,
    rule_id: str | None = None,
    extern_id: str | None = None,
    code: str | None = None,
    blocking: bool = True,
) -> CheckIssue:
    return CheckIssue(
        stage=stage,
        path=path,
        message=message,
        rule_id=rule_id,
        extern_id=extern_id,
        code=code,
        blocking=blocking,
    )


def type_from_spec(value: Any) -> TypeSpec:
    if isinstance(value, TypeSpec):
        return value
    if isinstance(value, str):
        return _type_from_kind(value, {})
    if isinstance(value, Mapping):
        raw_kind = value.get("kind", value.get("type", "any"))
        return _type_from_kind(str(raw_kind or "any"), value)
    return ANY


def _type_from_kind(kind: str, spec: Mapping[str, Any]) -> TypeSpec:
    normalized = {
        "boolean": "bool",
        "bool": "bool",
        "integer": "int",
        "int": "int",
        "uint": "uint",
        "unsigned": "uint",
        "bit": "bitvector",
        "bits": "bitvector",
        "bitvector": "bitvector",
        "enum": "enum",
        "hex": "string",
        "hex_string": "string",
        "string": "string",
        "bytes": "bytes",
        "any": "any",
    }.get(kind, kind or "any")
    width = spec.get("width")
    if width is None and normalized == "bitvector":
        width = spec.get("bits")
    width_value = int(width) if isinstance(width, int) and width > 0 else None
    choices = spec.get("choices", ())
    if not isinstance(choices, list | tuple):
        choices = ()
    fmt = spec.get("format")
    if kind in {"hex", "hex_string"} and fmt is None:
        fmt = "hex"
    return TypeSpec(
        normalized,
        width=width_value,
        choices=tuple(choices),
        format=str(fmt) if fmt is not None else None,
    )


def type_compatible(actual: TypeSpec, expected: TypeSpec) -> bool:
    if actual.is_any or expected.is_any:
        return True
    if actual.kind == expected.kind:
        if actual.kind == "bitvector":
            return (
                actual.width is None
                or expected.width is None
                or actual.width == expected.width
            )
        if actual.kind == "enum":
            return not actual.choices or not expected.choices or actual.choices == expected.choices
        if actual.kind == "string":
            return actual.format is None or expected.format is None or actual.format == expected.format
        return True
    if {actual.kind, expected.kind} == {"enum", "string"}:
        return True
    if actual.kind in {"int", "uint", "bitvector"} and expected.kind in {"int", "uint", "bitvector"}:
        if actual.kind == "bitvector" and expected.kind == "bitvector":
            return (
                actual.width is None
                or expected.width is None
                or actual.width == expected.width
            )
        return True
    return False


def merge_numeric(left: TypeSpec, right: TypeSpec) -> TypeSpec:
    if left.is_any:
        return right
    if right.is_any:
        return left
    if left.kind == "bitvector" or right.kind == "bitvector":
        if left.kind == right.kind and left.width == right.width:
            return TypeSpec("bitvector", width=left.width)
        if left.kind == "bitvector" and right.kind in {"int", "uint"}:
            return TypeSpec("bitvector", width=left.width)
        if right.kind == "bitvector" and left.kind in {"int", "uint"}:
            return TypeSpec("bitvector", width=right.width)
        return ANY
    if left.kind == "uint" and right.kind == "uint":
        return UINT
    if left.kind in {"int", "uint"} and right.kind in {"int", "uint"}:
        return INT
    return ANY
