"""Spec-to-IR generation stages."""

from .semantic_ir import (
    SEMANTIC_SPEC_IR_SCHEMA_VERSION,
    SemanticSpecIRCallable,
    SemanticSpecIRIssue,
    SemanticSpecIRValidationError,
    build_semantic_spec_ir_prompt,
    collect_semantic_spec_ir_issues,
    generate_semantic_spec_ir,
    invoke_semantic_spec_ir_backend,
    load_semantic_spec_ir,
    maybe_call_semantic_spec_ir_llm,
    normalize_semantic_spec_ir_response,
    validate_semantic_spec_ir,
    write_semantic_spec_ir,
)

__all__ = [
    "SEMANTIC_SPEC_IR_SCHEMA_VERSION",
    "SemanticSpecIRCallable",
    "SemanticSpecIRIssue",
    "SemanticSpecIRValidationError",
    "build_semantic_spec_ir_prompt",
    "collect_semantic_spec_ir_issues",
    "generate_semantic_spec_ir",
    "invoke_semantic_spec_ir_backend",
    "load_semantic_spec_ir",
    "maybe_call_semantic_spec_ir_llm",
    "normalize_semantic_spec_ir_response",
    "validate_semantic_spec_ir",
    "write_semantic_spec_ir",
]
