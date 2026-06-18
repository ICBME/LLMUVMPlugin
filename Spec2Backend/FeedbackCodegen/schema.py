"""Shared schemas for feedback-driven LLM code generation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


class FeedbackCodegenError(ValueError):
    """Raised when a feedback-codegen artifact or configuration is invalid."""


@dataclass(frozen=True)
class GeneratedFile:
    path: str
    content: str

    def __post_init__(self) -> None:
        path = normalize_bundle_path(self.path)
        if not isinstance(self.content, str):
            raise FeedbackCodegenError("generated file content must be text")
        object.__setattr__(self, "path", path)

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content}


@dataclass(frozen=True)
class GeneratedFileBundle:
    """File bundle emitted by an LLM/codegen stage."""

    files: tuple[GeneratedFile, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        files = tuple(_normalize_file(item) for item in self.files)
        if not files:
            raise FeedbackCodegenError("generated file bundle must contain at least one file")
        metadata = _json_mapping(self.metadata, spec="generated file bundle metadata")
        assumptions = tuple(str(item) for item in self.assumptions)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "assumptions", assumptions)

    @classmethod
    def from_dict(cls, value: Any) -> "GeneratedFileBundle":
        if not isinstance(value, Mapping):
            raise FeedbackCodegenError("generated file bundle must be a JSON object")
        files = value.get("files")
        if not isinstance(files, list):
            raise FeedbackCodegenError("generated file bundle must define files[]")
        assumptions = value.get("assumptions", ())
        if assumptions is None:
            assumptions = ()
        if not isinstance(assumptions, list | tuple):
            raise FeedbackCodegenError("generated file bundle assumptions must be a list")
        return cls(
            files=tuple(files),
            metadata=value.get("metadata", {}),
            assumptions=tuple(str(item) for item in assumptions),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "files": [item.to_json() for item in self.files],
            "metadata": dict(self.metadata),
            "assumptions": list(self.assumptions),
        }

    def write_to(self, directory: str | Path) -> tuple[Path, ...]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for item in self.files:
            output = root / item.path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(item.content, encoding="utf-8")
            written.append(output)
        return tuple(written)


@dataclass(frozen=True)
class CodegenEvaluationIssue:
    stage: str
    message: str
    severity: str = "error"
    path: str | None = None
    blocking: bool = True
    rule_id: str | None = None
    semantic_element_id: str | None = None
    case_id: str | None = None
    expected: Any = None
    actual: Any = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stage": self.stage,
            "severity": self.severity,
            "message": self.message,
            "blocking": self.blocking,
        }
        for key in ("path", "rule_id", "semantic_element_id", "case_id"):
            item = getattr(self, key)
            if item is not None:
                value[key] = item
        if self.expected is not None:
            value["expected"] = _json_safe(self.expected)
        if self.actual is not None:
            value["actual"] = _json_safe(self.actual)
        return value


@dataclass(frozen=True)
class CodegenEvaluation:
    passed: bool
    issues: tuple[CodegenEvaluationIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issues",
            tuple(_normalize_issue(item) for item in self.issues),
        )
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="codegen evaluation metadata"),
        )

    @classmethod
    def passed_result(cls, **metadata: Any) -> "CodegenEvaluation":
        return cls(passed=True, metadata=metadata)

    @classmethod
    def failed(cls, *issues: CodegenEvaluationIssue, **metadata: Any) -> "CodegenEvaluation":
        return cls(passed=False, issues=tuple(issues), metadata=metadata)

    @classmethod
    def from_dict(cls, value: Any) -> "CodegenEvaluation":
        if not isinstance(value, Mapping):
            raise FeedbackCodegenError("codegen evaluation must be a mapping")
        issues = value.get("issues", ())
        if not isinstance(issues, list | tuple):
            raise FeedbackCodegenError("codegen evaluation issues must be a list")
        return cls(
            passed=bool(value.get("passed", False)),
            issues=tuple(issues),
            metadata=value.get("metadata", {}),
        )

    @property
    def blocking_issues(self) -> tuple[CodegenEvaluationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    def to_json(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [item.to_json() for item in self.issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class GoldenCase:
    target: str
    data: Mapping[str, Any]
    expected: Any
    case_id: str = ""
    line_no: int = 1

    @classmethod
    def from_value(cls, value: Any, *, default_target: str) -> "GoldenCase":
        if isinstance(value, GoldenCase):
            return value
        if not isinstance(value, Mapping):
            raise FeedbackCodegenError("golden case must be a mapping")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise FeedbackCodegenError("golden case must define data mapping")
        target = str(value.get("target") or data.get("target") or default_target)
        if "expected" not in value:
            raise FeedbackCodegenError("golden case must define expected")
        return cls(
            target=target,
            data=dict(data),
            expected=value["expected"],
            case_id=str(value.get("case_id") or value.get("id") or ""),
            line_no=int(value.get("line_no") or 1),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "data": dict(self.data),
            "expected": self.expected,
            "case_id": self.case_id,
            "line_no": self.line_no,
        }


@dataclass(frozen=True)
class CodegenLoopConfig:
    output_dir: Path
    max_attempts: int = 3
    run_name: str = "feedback_codegen"
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    topology_out: Path | None = None
    observation_context: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(self, "max_attempts", max(1, int(self.max_attempts)))
        object.__setattr__(self, "tags", tuple(str(item) for item in self.tags))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="codegen loop metadata"),
        )
        if self.topology_out is not None:
            object.__setattr__(self, "topology_out", Path(self.topology_out))


@dataclass(frozen=True)
class CodegenAttempt:
    index: int
    status: str
    attempt_dir: Path
    prompt_path: Path
    response_path: Path
    candidate_bundle_path: Path
    candidate_dir: Path
    evaluation_path: Path
    feedback_path: Path
    evaluation: CodegenEvaluation

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status,
            "attempt_dir": str(self.attempt_dir),
            "prompt_path": str(self.prompt_path),
            "response_path": str(self.response_path),
            "candidate_bundle_path": str(self.candidate_bundle_path),
            "candidate_dir": str(self.candidate_dir),
            "evaluation_path": str(self.evaluation_path),
            "feedback_path": str(self.feedback_path),
            "evaluation": self.evaluation.to_json(),
        }


@dataclass(frozen=True)
class CodegenResult:
    status: str
    attempts: tuple[CodegenAttempt, ...]
    final_dir: Path | None = None
    final_paths: tuple[Path, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": [item.to_json() for item in self.attempts],
            "final_dir": str(self.final_dir) if self.final_dir is not None else None,
            "final_paths": [str(path) for path in self.final_paths],
            "metadata": dict(self.metadata),
        }


def normalize_bundle_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise FeedbackCodegenError("generated file path must be a non-empty string")
    if "\\" in path:
        raise FeedbackCodegenError(f"generated file path must use POSIX separators: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise FeedbackCodegenError(f"generated file path must be relative: {path!r}")
    if ".." in pure.parts:
        raise FeedbackCodegenError(f"generated file path must not escape artifact dir: {path!r}")
    if any(part in {"", "."} for part in pure.parts):
        raise FeedbackCodegenError(f"generated file path has invalid segment: {path!r}")
    return pure.as_posix()


def write_json(path: str | Path, value: Any) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = value.to_json() if hasattr(value, "to_json") else value
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _normalize_file(value: Any) -> GeneratedFile:
    if isinstance(value, GeneratedFile):
        return value
    if not isinstance(value, Mapping):
        raise FeedbackCodegenError("generated file entry must be a JSON object")
    return GeneratedFile(path=value.get("path"), content=value.get("content"))


def _normalize_issue(value: Any) -> CodegenEvaluationIssue:
    if isinstance(value, CodegenEvaluationIssue):
        return value
    if not isinstance(value, Mapping):
        raise FeedbackCodegenError("evaluation issue must be a mapping")
    return CodegenEvaluationIssue(
        stage=str(value.get("stage") or ""),
        message=str(value.get("message") or ""),
        severity=str(value.get("severity") or "error"),
        path=str(value["path"]) if value.get("path") is not None else None,
        blocking=bool(value.get("blocking", True)),
        rule_id=str(value["rule_id"]) if value.get("rule_id") is not None else None,
        semantic_element_id=(
            str(value["semantic_element_id"])
            if value.get("semantic_element_id") is not None
            else None
        ),
        case_id=str(value["case_id"]) if value.get("case_id") is not None else None,
        expected=value.get("expected"),
        actual=value.get("actual"),
    )


def _json_mapping(value: Any, *, spec: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FeedbackCodegenError(f"{spec} must be a mapping")
    result = dict(value)
    try:
        json.dumps(result)
    except (TypeError, ValueError) as exc:
        raise FeedbackCodegenError(f"{spec} must be JSON-serializable: {exc}") from exc
    return _json_safe(result)


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=repr))
