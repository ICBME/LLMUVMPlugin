"""Coverage and LLM-guided mutation feedback for LibAFL BFM fuzzing."""

from .coverage import build_summary
from .advisors import propose_directives, validate_directives
from .rtl_structure_coverage import (
    RTL_STRUCTURAL_COVERAGE_DOMAIN,
    RTL_STRUCTURAL_COVERAGE_KINDS,
    build_rtl_structure_coverage,
)

__all__ = [
    "RTL_STRUCTURAL_COVERAGE_DOMAIN",
    "RTL_STRUCTURAL_COVERAGE_KINDS",
    "build_rtl_structure_coverage",
    "build_summary",
    "propose_directives",
    "validate_directives",
]
