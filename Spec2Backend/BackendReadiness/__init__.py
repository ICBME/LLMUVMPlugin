"""Backend readiness analysis for SemanticSpecIR lowering."""

from .readiness import (
    BACKEND_READINESS_SCHEMA_VERSION,
    analyze_backend_readiness,
    load_backend_readiness,
    write_backend_readiness,
)

__all__ = [
    "BACKEND_READINESS_SCHEMA_VERSION",
    "analyze_backend_readiness",
    "load_backend_readiness",
    "write_backend_readiness",
]
