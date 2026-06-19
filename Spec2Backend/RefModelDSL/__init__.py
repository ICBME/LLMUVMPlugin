"""RefModelIR/DSL generation, execution, and verification."""

from .codegen import RefModelIRCodegenTask, generate_ref_model_ir_with_feedback
from .emit_plugin import emit_ref_model_plugin, ref_model_plugin_spec
from .extern import ExternRegistry
from .interpreter import RefModelInterpreter
from .schema import (
    REF_MODEL_IR_SCHEMA_VERSION,
    RefModelDSLError,
    VerificationIssue,
    VerificationReport,
    normalize_ref_model_ir,
)
from .verifier import verify_ref_model_ir

__all__ = [
    "ExternRegistry",
    "REF_MODEL_IR_SCHEMA_VERSION",
    "RefModelDSLError",
    "RefModelIRCodegenTask",
    "RefModelInterpreter",
    "VerificationIssue",
    "VerificationReport",
    "emit_ref_model_plugin",
    "generate_ref_model_ir_with_feedback",
    "normalize_ref_model_ir",
    "ref_model_plugin_spec",
    "verify_ref_model_ir",
]
