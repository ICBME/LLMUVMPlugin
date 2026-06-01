"""Coverage and LLM-guided mutation feedback for LibAFL BFM fuzzing."""

from .coverage import build_summary
from .coverage_export import CoverageExport, CoveragePoint
from .advisors import propose_directives, validate_directives
from .rtl_gap import RTL_GAP_DOMAIN, build_rtl_gap_export, build_rtl_gap_summary
from .rtl_structure_coverage import (
    RTL_STRUCTURAL_COVERAGE_DOMAIN,
    RTL_STRUCTURAL_COVERAGE_KINDS,
    build_rtl_structure_coverage,
    build_rtl_structure_coverage_export,
)

__all__ = [
    "CoverageExport",
    "CoveragePoint",
    "RTL_GAP_DOMAIN",
    "RTL_STRUCTURAL_COVERAGE_DOMAIN",
    "RTL_STRUCTURAL_COVERAGE_KINDS",
    "build_rtl_gap_export",
    "build_rtl_gap_summary",
    "build_rtl_structure_coverage",
    "build_rtl_structure_coverage_export",
    "build_summary",
    "propose_directives",
    "validate_directives",
]
