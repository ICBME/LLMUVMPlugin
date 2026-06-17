from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
import shlex
from pathlib import Path
from typing import Protocol


def split_tool_command(value: str, default: str) -> list[str]:
    return shlex.split(value) or [default]


def path_from_cwd(path: Path | str | None, cwd: Path | str | None) -> Path | None:
    if path is None:
        return None
    resolved = Path(path)
    if resolved.is_absolute() or cwd is None:
        return resolved
    if not str(cwd).strip():
        return resolved
    base = Path(cwd)
    if not base.is_absolute():
        base = Path.cwd() / base
    return base / resolved


def run_cwd(cwd: Path | str | None) -> Path:
    if cwd is None:
        return Path.cwd()
    if not str(cwd).strip():
        return Path.cwd()
    resolved = Path(cwd)
    return resolved if resolved.is_absolute() else Path.cwd() / resolved


def resolved_artifact_path(value: object, *, cwd: Path | str | None = None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute() or cwd is None:
        return path
    return Path(cwd) / path


def manifest_path_from_value(
    value: object,
    *,
    manifest_path: Path,
    cwd: Path | str | None = None,
) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    if cwd is not None and str(cwd).strip():
        return Path(cwd) / path
    return manifest_path.parent / path


class FuzzRunConfigView(Protocol):
    target: str
    corpus: Path
    libafl_manifest: Path
    target_config: Path | None
    directives: Path | None
    iters: int
    max_seeds: int
    seed: int
    cargo: str
    cwd: Path | None
    topology_out: Path | None
    make: str
    verilog_sources: str | None
    toplevel: str | None
    functional_coverage: Path | None
    feedback_functional_coverage: Path | None
    verilator_coverage: str
    extra_make_vars: tuple[str, ...]
    directives_out: Path | None
    heuristic_directives_out: Path | None
    feedback_corpus: Path | None
    observation_out: Path | None
    monitoring_out: Path | None


@dataclass
class RunPathResolver:
    config: FuzzRunConfigView
    artifacts: MutableMapping[str, Path] = field(default_factory=dict)

    def path_from_cwd(self, path: Path | None) -> Path | None:
        return path_from_cwd(path, self.config.cwd)

    def run_cwd(self) -> Path:
        return run_cwd(self.config.cwd)

    def required_artifact(self, role: str) -> Path:
        try:
            path = self.artifacts[role]
        except KeyError as exc:
            raise ValueError(f"missing required artifact role: {role}") from exc
        if path is None:
            raise ValueError(f"missing required artifact role: {role}")
        return path

    def optional_artifact(self, role: str) -> Path | None:
        return self.artifacts.get(role)

    def make_value(self, path: Path | None) -> str:
        resolved = self.path_from_cwd(path)
        return str(resolved) if resolved is not None else ""

    def feedback_corpus(self) -> Path:
        path = self.path_from_cwd(self.config.feedback_corpus)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: feedback_corpus"
            )
        return path

    def feedback_directives(self) -> Path:
        path = self.path_from_cwd(self.config.directives_out)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: directives_out"
            )
        return path

    def feedback_functional_coverage(self) -> Path | None:
        if self.config.feedback_functional_coverage is not None:
            return self.path_from_cwd(self.config.feedback_functional_coverage)
        if self.config.functional_coverage is None or self.config.feedback_corpus is None:
            return None
        functional_coverage = self.path_from_cwd(self.config.functional_coverage)
        if functional_coverage is None:
            return None
        return functional_coverage.with_name(
            f"{functional_coverage.stem}_feedback{functional_coverage.suffix}"
        )

    def heuristic_directives(self) -> Path | None:
        if self.config.heuristic_directives_out is not None:
            return self.path_from_cwd(self.config.heuristic_directives_out)
        if self.config.directives_out is None:
            return None
        directives = self.path_from_cwd(self.config.directives_out)
        if directives is None:
            return None
        return directives.with_name(f"{directives.stem}_heuristic{directives.suffix}")

    def manifest_path(self, path: Path | None) -> str | None:
        resolved = self.path_from_cwd(path)
        return str(resolved) if resolved is not None else None

    def artifact_exists(self, path: Path | None) -> bool:
        resolved = self.path_from_cwd(path)
        return resolved.exists() if resolved is not None else False


__all__ = [
    "FuzzRunConfigView",
    "RunPathResolver",
    "manifest_path_from_value",
    "path_from_cwd",
    "resolved_artifact_path",
    "run_cwd",
    "split_tool_command",
]
