"""Schemas and JSON helpers for reference-model DSL artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


REF_MODEL_IR_SCHEMA_VERSION = 1


class RefModelDSLError(ValueError):
    """Raised when a RefModelIR/DSL artifact is invalid."""


@dataclass(frozen=True)
class VerificationIssue:
    stage: str
    message: str
    severity: str = "error"
    path: str | None = None
    blocking: bool = True
    rule_id: str | None = None
    extern_id: str | None = None

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
        return value


@dataclass(frozen=True)
class VerificationReport:
    status: str
    verification_level: str
    proved_rules: tuple[str, ...] = ()
    trusted_standard_externs: tuple[str, ...] = ()
    tested_externs: tuple[str, ...] = ()
    issues: tuple[VerificationIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "proved_rules", tuple(str(item) for item in self.proved_rules))
        object.__setattr__(
            self,
            "trusted_standard_externs",
            tuple(str(item) for item in self.trusted_standard_externs),
        )
        object.__setattr__(self, "tested_externs", tuple(str(item) for item in self.tested_externs))
        object.__setattr__(self, "issues", tuple(_normalize_issue(item) for item in self.issues))
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, spec="verification metadata"))

    @property
    def passed(self) -> bool:
        return self.status == "passed" and not self.blocked_issues

    @property
    def blocked_issues(self) -> tuple[VerificationIssue, ...]:
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


def normalize_ref_model_ir(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RefModelDSLError("RefModelIR must be a JSON object")
    result = _json_mapping(value, spec="RefModelIR")
    if result.get("schema_version") != REF_MODEL_IR_SCHEMA_VERSION:
        raise RefModelDSLError(
            f"RefModelIR schema_version must be {REF_MODEL_IR_SCHEMA_VERSION}"
        )
    return result


def normalize_extern_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise RefModelDSLError("extern path must be a non-empty string")
    if "\\" in path:
        raise RefModelDSLError(f"extern path must use POSIX separators: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or any(part in {"", "."} for part in pure.parts):
        raise RefModelDSLError(f"extern path must stay within artifact directory: {path!r}")
    return pure.as_posix()


def write_json(path: str | Path, value: Any) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = value.to_json() if hasattr(value, "to_json") else value
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=repr))


def _normalize_issue(value: VerificationIssue | Mapping[str, Any]) -> VerificationIssue:
    if isinstance(value, VerificationIssue):
        return value
    if not isinstance(value, Mapping):
        raise RefModelDSLError("verification issue must be a mapping")
    return VerificationIssue(
        stage=str(value.get("stage") or ""),
        message=str(value.get("message") or ""),
        severity=str(value.get("severity") or "error"),
        path=str(value["path"]) if value.get("path") is not None else None,
        blocking=bool(value.get("blocking", True)),
        rule_id=str(value["rule_id"]) if value.get("rule_id") is not None else None,
        extern_id=str(value["extern_id"]) if value.get("extern_id") is not None else None,
    )


def _json_mapping(value: Any, *, spec: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RefModelDSLError(f"{spec} must be a mapping")
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise RefModelDSLError(f"{spec} must be JSON-serializable: {exc}") from exc


__all__ = [
    "REF_MODEL_IR_SCHEMA_VERSION",
    "RefModelDSLError",
    "VerificationIssue",
    "VerificationReport",
    "json_safe",
    "normalize_extern_path",
    "normalize_ref_model_ir",
    "read_json",
    "write_json",
]
