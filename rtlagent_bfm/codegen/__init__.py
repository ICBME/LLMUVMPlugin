"""Helpers for the first LLM-generated plugin workflow.

This package keeps LLM-generated reference model and scoreboard artifacts
outside the replay core. The intended flow is:

1. Build a prompt from a target manifest, generated BFM IR, and spec files.
2. Write the LLM file bundle into a candidate directory.
3. Validate static safety, plugin contracts, and optional golden cases.
4. Promote the validated candidate into a final artifact directory.
"""

from .artifacts import ArtifactBundle, GeneratedFile
from .manifest import update_manifest_plugins
from .pipeline import (
    CodegenPipelineConfig,
    finalize_bundle,
    promote_candidate,
    write_candidate_bundle,
)
from .prompt import build_generation_prompt, write_generation_prompt
from .validation import GoldenCase, validate_artifact_dir

__all__ = [
    "ArtifactBundle",
    "CodegenPipelineConfig",
    "GeneratedFile",
    "GoldenCase",
    "build_generation_prompt",
    "finalize_bundle",
    "promote_candidate",
    "update_manifest_plugins",
    "validate_artifact_dir",
    "write_candidate_bundle",
    "write_generation_prompt",
]
