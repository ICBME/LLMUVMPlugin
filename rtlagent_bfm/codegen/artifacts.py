"""Generated artifact bundle handling."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


class ArtifactBundleError(ValueError):
    """Raised when an LLM artifact bundle is malformed or unsafe to write."""


@dataclass(frozen=True)
class GeneratedFile:
    """One generated text file from an LLM response bundle."""

    path: str
    content: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeneratedFile":
        path = data.get("path")
        content = data.get("content")
        if not isinstance(path, str) or not path:
            raise ArtifactBundleError("generated file must define a non-empty path")
        if not isinstance(content, str):
            raise ArtifactBundleError(f"{path}: generated file content must be a string")
        _safe_relative_path(path)
        return cls(path=path, content=content)


@dataclass(frozen=True)
class ArtifactBundle:
    """A JSON-serializable bundle returned by the LLM."""

    files: tuple[GeneratedFile, ...]
    assumptions: tuple[str, ...] = ()
    required_tests: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactBundle":
        files = data.get("files")
        if not isinstance(files, list) or not files:
            raise ArtifactBundleError("artifact bundle must contain a non-empty files list")
        generated_files = tuple(GeneratedFile.from_dict(item) for item in files)
        return cls(
            files=generated_files,
            assumptions=_string_tuple(data.get("assumptions", ()), "assumptions"),
            required_tests=_string_tuple(data.get("required_tests", ()), "required_tests"),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ArtifactBundle":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def write_to(self, root: str | Path) -> list[Path]:
        """Write bundle files under ``root`` and return written paths."""

        root_path = Path(root)
        written: list[Path] = []
        for generated_file in self.files:
            relative_path = _safe_relative_path(generated_file.path)
            output_path = root_path / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(generated_file.content, encoding="utf-8")
            written.append(output_path)
        return written


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ArtifactBundleError(f"{name} must be a list of strings")
    result = tuple(str(item) for item in value)
    return result


def _safe_relative_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        raise ArtifactBundleError(f"generated path must be relative: {path}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ArtifactBundleError(f"generated path must not escape the artifact root: {path}")
    return candidate
