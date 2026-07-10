"""Spec-to-IR generation stages.

Exports are resolved lazily so backend-only users can import Spec2Backend even
when optional/deprecated Spec2IR generation dependencies are not installed.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_MODULES = {
    "REVIEW_SCHEMA_VERSION": ".validation_review",
    "REPRESENTATION_AST_VERSION": ".representation_ast",
    "SEMANTIC_SPEC_IR_SCHEMA_VERSION": ".semantic_ir",
    "ReviewFinding": ".validation_review",
    "SemanticSpecIRCallable": ".semantic_ir",
    "SemanticSpecIRIssue": ".semantic_ir",
    "SemanticSpecIRValidationError": ".semantic_ir",
    "AutomationDecision": ".automation",
    "AutomationPolicyGraph": ".automation",
    "AutomationPolicyRule": ".automation",
    "Spec2IRHarness": ".harness",
    "SemanticIRPatchError": ".patch",
    "apply_semantic_ir_patch": ".patch",
    "classify_review_for_automation": ".automation",
    "deterministic_repair_semantic_spec_ir": ".automation",
    "build_semantic_spec_ir_auto_formalization_prompt": ".semantic_repair",
    "build_semantic_spec_ir_repair_prompt": ".semantic_repair",
    "build_semantic_spec_ir_prompt": ".semantic_ir",
    "collect_obligation_coverage": ".semantic_obligation_coverage",
    "collect_representation_completeness_issues": ".representation_ast",
    "collect_semantic_spec_ir_issues": ".semantic_ir",
    "collect_source_claim_coverage": ".claim_extraction",
    "generate_semantic_spec_ir": ".semantic_ir",
    "invoke_semantic_spec_ir_backend": ".semantic_ir",
    "load_semantic_spec_ir": ".semantic_ir",
    "maybe_call_semantic_spec_ir_llm": ".semantic_ir",
    "normalize_semantic_spec_ir_response": ".semantic_ir",
    "repair_semantic_spec_ir_file": ".semantic_repair",
    "repair_semantic_spec_ir_with_review": ".semantic_repair",
    "representation_ast_contract": ".representation_ast",
    "review_semantic_spec_ir": ".validation_review",
    "review_semantic_spec_ir_file": ".validation_review",
    "run_spec2ir_agent": ".harness",
    "semantic_ir_patch_contract": ".patch",
    "semantic_ir_patch_from_candidate": ".patch",
    "semantic_ir_sha256": ".patch",
    "validate_semantic_spec_ir": ".semantic_ir",
    "write_semantic_repair_artifacts": ".semantic_repair",
    "write_semantic_review": ".validation_review",
    "write_semantic_spec_ir": ".semantic_ir",
}

__all__ = sorted(_EXPORT_MODULES)


def __getattr__(name: str) -> Any:
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
