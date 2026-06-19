"""Reusable semantic/type/formal checks for Spec2Backend IRs."""

from .adapters import RefModelIRAdapter, SemanticSpecIRAdapter
from .engine import run_checks
from .model import (
    ANY,
    BOOL,
    INT,
    STRING,
    UINT,
    AssignmentCheck,
    CheckContext,
    CheckIssue,
    CheckReport,
    EquivalenceCheck,
    ExternCheck,
    ExpressionCheck,
    FormalObligation,
    PredicateCheck,
    Symbol,
    TotalityCheck,
    TypeSpec,
)

__all__ = [
    "ANY",
    "BOOL",
    "INT",
    "STRING",
    "UINT",
    "AssignmentCheck",
    "CheckContext",
    "CheckIssue",
    "CheckReport",
    "EquivalenceCheck",
    "ExternCheck",
    "ExpressionCheck",
    "FormalObligation",
    "PredicateCheck",
    "RefModelIRAdapter",
    "SemanticSpecIRAdapter",
    "Symbol",
    "TotalityCheck",
    "TypeSpec",
    "run_checks",
]
