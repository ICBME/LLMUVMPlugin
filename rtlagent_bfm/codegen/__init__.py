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
from .oracle_feedback import (
    build_oracle_ir_repair_prompt,
    normalize_oracle_ir_response,
    repair_oracle_ir_with_feedback,
)
from .oracle_ir import (
    OracleIRValidationError,
    collect_oracle_ir_issues,
    generate_oracle_ir,
    validate_oracle_ir,
)
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
    "OracleIRValidationError",
    "build_generation_prompt",
    "build_oracle_ir_repair_prompt",
    "collect_oracle_ir_issues",
    "finalize_bundle",
    "generate_oracle_ir",
    "normalize_oracle_ir_response",
    "promote_candidate",
    "repair_oracle_ir_with_feedback",
    "update_manifest_plugins",
    "validate_artifact_dir",
    "validate_oracle_ir",
    "write_candidate_bundle",
    "write_generation_prompt",
]
