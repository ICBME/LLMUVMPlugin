"""Lightweight candidate validation and promotion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .artifacts import ArtifactBundle
from .manifest import update_manifest_plugins
from .validation import GoldenCase, validate_artifact_dir


@dataclass(frozen=True)
class CodegenPipelineConfig:
    """Configuration for promoting an LLM candidate into final artifacts."""

    bundle_path: Path
    candidate_dir: Path
    final_dir: Path
    target: str
    ref_model: str | None = None
    scoreboard: str | None = None
    manifest_path: Path | None = None
    bfm_ir: str | None = None
    extra_python_paths: tuple[Path, ...] = ()
    golden_cases: tuple[GoldenCase, ...] = ()


def write_candidate_bundle(bundle_path: str | Path, candidate_dir: str | Path) -> list[Path]:
    bundle = ArtifactBundle.load(bundle_path)
    return bundle.write_to(candidate_dir)


def promote_candidate(candidate_dir: str | Path, final_dir: str | Path) -> list[Path]:
    """Copy a validated candidate directory into the final artifact directory."""

    source = Path(candidate_dir)
    destination = Path(final_dir)
    if not source.exists():
        raise FileNotFoundError(f"candidate directory does not exist: {source}")

    copied: list[Path] = []
    for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
        if "__pycache__" in source_file.parts or source_file.suffix == ".pyc":
            continue
        relative = source_file.relative_to(source)
        destination_file = destination / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)
        copied.append(destination_file)
    return copied


def finalize_bundle(config: CodegenPipelineConfig) -> list[Path]:
    """Write, validate, promote, and optionally connect a generated bundle."""

    write_candidate_bundle(config.bundle_path, config.candidate_dir)
    validate_artifact_dir(
        config.candidate_dir,
        target=config.target,
        ref_model=config.ref_model,
        scoreboard=config.scoreboard,
        extra_python_paths=config.extra_python_paths,
        golden_cases=config.golden_cases,
    )
    copied = promote_candidate(config.candidate_dir, config.final_dir)
    if config.manifest_path is not None:
        update_manifest_plugins(
            config.manifest_path,
            bfm_ir=config.bfm_ir,
            ref_model=config.ref_model,
            scoreboard=config.scoreboard,
        )
    return copied
