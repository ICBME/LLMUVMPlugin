"""Reference-model generation pipeline pieces.

This package hosts the new ref-model generation flow.  The existing
``rtlagent_bfm.codegen`` package remains as a compatibility shell while
generation logic moves here in smaller, auditable stages.
"""

from Spec2Backend.Spec2IR import (
    SEMANTIC_SPEC_IR_SCHEMA_VERSION,
    SemanticSpecIRIssue,
    SemanticSpecIRValidationError,
    build_semantic_spec_ir_prompt,
    collect_semantic_spec_ir_issues,
    generate_semantic_spec_ir,
    load_semantic_spec_ir,
    maybe_call_semantic_spec_ir_llm,
    normalize_semantic_spec_ir_response,
    review_semantic_spec_ir,
    review_semantic_spec_ir_file,
    validate_semantic_spec_ir,
    write_semantic_review,
    write_semantic_spec_ir,
)

__all__ = [
    "SEMANTIC_SPEC_IR_SCHEMA_VERSION",
    "SemanticSpecIRIssue",
    "SemanticSpecIRValidationError",
    "build_semantic_spec_ir_prompt",
    "collect_semantic_spec_ir_issues",
    "generate_semantic_spec_ir",
    "load_semantic_spec_ir",
    "maybe_call_semantic_spec_ir_llm",
    "normalize_semantic_spec_ir_response",
    "review_semantic_spec_ir",
    "review_semantic_spec_ir_file",
    "validate_semantic_spec_ir",
    "write_semantic_review",
    "write_semantic_spec_ir",
]
