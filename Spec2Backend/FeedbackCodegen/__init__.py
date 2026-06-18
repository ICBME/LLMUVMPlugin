"""Feedback-driven LLM code generation for Spec2Backend artifacts."""

from .loop import FeedbackCodegenTask, generate_with_feedback, llm_response_to_json
from .ref_model import (
    RefModelCodegenTask,
    build_candidate_ref_model,
    generate_ref_model_with_feedback,
    golden_case_issues,
    normalize_generated_file_bundle_response,
    static_validation_issues,
)
from .schema import (
    CodegenAttempt,
    CodegenEvaluation,
    CodegenEvaluationIssue,
    CodegenLoopConfig,
    CodegenResult,
    FeedbackCodegenError,
    GeneratedFile,
    GeneratedFileBundle,
    GoldenCase,
)
from .topology import FEEDBACK_CODEGEN_TOPOLOGY

__all__ = [
    "CodegenAttempt",
    "CodegenEvaluation",
    "CodegenEvaluationIssue",
    "CodegenLoopConfig",
    "CodegenResult",
    "FEEDBACK_CODEGEN_TOPOLOGY",
    "FeedbackCodegenError",
    "FeedbackCodegenTask",
    "GeneratedFile",
    "GeneratedFileBundle",
    "GoldenCase",
    "RefModelCodegenTask",
    "build_candidate_ref_model",
    "generate_ref_model_with_feedback",
    "generate_with_feedback",
    "golden_case_issues",
    "llm_response_to_json",
    "normalize_generated_file_bundle_response",
    "static_validation_issues",
]
